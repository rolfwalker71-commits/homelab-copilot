"""SQLite-backed topology cache with in-memory hot path."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from app.core.locale import format_de, iso_utc, now_berlin
from app.core.models import EntityStatus, TopologySnapshot
from app.core.reconcile import ReconcileStats, reconcile_topology

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS topology_snapshots (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL,
    refreshed_at TEXT NOT NULL,
    refreshed_at_iso TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL
);
"""


class TopologyStore:
    """Keeps the latest topology in memory and persists it to SQLite."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._snapshot: TopologySnapshot | None = None
        self._db: aiosqlite.Connection | None = None

    @property
    def snapshot(self) -> TopologySnapshot | None:
        return self._snapshot

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        await self._load_latest()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _load_latest(self) -> None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT payload FROM topology_snapshots WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return
        try:
            self._snapshot = TopologySnapshot.model_validate_json(row[0])
            logger.info(
                "Loaded cached topology from %s",
                self._snapshot.refreshed_at,
            )
        except Exception:
            logger.exception("Failed to parse cached topology")

    async def apply_live(
        self, live: TopologySnapshot
    ) -> tuple[TopologySnapshot, ReconcileStats]:
        """Reconcile live PVE discovery against the cached snapshot, then persist."""
        snapshot, stats = reconcile_topology(self._snapshot, live)
        await self.save(snapshot)
        return snapshot, stats

    def _status_from_live(self, value: str | None) -> EntityStatus | None:
        if not value:
            return None
        v = str(value).strip().lower()
        if v in {"running", "online"}:
            return EntityStatus.RUNNING
        if v in {"stopped", "offline"}:
            return EntityStatus.STOPPED
        if v in {"paused", "suspended"}:
            return EntityStatus.PAUSED
        if v == "error":
            return EntityStatus.ERROR
        return EntityStatus.UNKNOWN

    async def patch_guest_live(self, live: dict[str, Any]) -> TopologySnapshot | None:
        """Update one guest's power/metrics in the cached snapshot (no Discovery)."""
        snap = self._snapshot
        if snap is None:
            return None
        gid = str(live.get("guest_id") or "").strip()
        if not gid:
            return snap
        status = self._status_from_live(live.get("status") if isinstance(live.get("status"), str) else None)
        guests: list = []
        changed = False
        for g in snap.guests:
            if g.id != gid:
                guests.append(g)
                continue
            meta = dict(g.meta or {})
            for key in (
                "uptime",
                "cpu",
                "cpu_pct",
                "cpus",
                "mem",
                "maxmem",
                "mem_pct",
                "disk",
                "maxdisk",
                "disk_pct",
                "lock",
            ):
                if key in live:
                    meta[key] = live[key]
            if live.get("unprivileged") is not None:
                meta["unprivileged"] = live["unprivileged"]
            updates: dict[str, Any] = {"meta": meta}
            if status is not None:
                updates["status"] = status
            guests.append(g.model_copy(update=updates))
            changed = True
        if not changed:
            return snap
        new_snap = snap.model_copy(update={"guests": guests})
        await self.save(new_snap)
        return new_snap

    async def save(self, snapshot: TopologySnapshot) -> None:
        self._snapshot = snapshot
        assert self._db is not None
        await self._db.execute(
            """
            INSERT INTO topology_snapshots (id, payload, refreshed_at, refreshed_at_iso)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payload = excluded.payload,
                refreshed_at = excluded.refreshed_at,
                refreshed_at_iso = excluded.refreshed_at_iso
            """,
            (snapshot.model_dump_json(), snapshot.refreshed_at, snapshot.refreshed_at_iso),
        )
        await self._db.commit()

    async def log(self, level: str, message: str) -> None:
        assert self._db is not None
        now = now_berlin()
        await self._db.execute(
            """
            INSERT INTO discovery_log (level, message, created_at, created_at_iso)
            VALUES (?, ?, ?, ?)
            """,
            (level, message, format_de(now), iso_utc(now)),
        )
        await self._db.commit()

    async def recent_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            """
            SELECT level, message, created_at, created_at_iso
            FROM discovery_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "level": r[0],
                "message": r[1],
                "created_at": r[2],
                "created_at_iso": r[3],
            }
            for r in rows
        ]

    def empty_snapshot(self, *, proxmox_configured: bool, errors: list[str] | None = None) -> TopologySnapshot:
        now = now_berlin()
        return TopologySnapshot(
            refreshed_at=format_de(now),
            refreshed_at_iso=iso_utc(now),
            proxmox_configured=proxmox_configured,
            errors=errors or [],
        )
