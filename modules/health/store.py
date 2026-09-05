"""SQLite store for HTTP(S) health checks and disk-alert de-dupe."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiosqlite

from app.core.locale import format_de, iso_utc, now_berlin

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'manual',
    last_status TEXT NOT NULL DEFAULT 'unknown',
    last_http_code INTEGER,
    last_error TEXT,
    last_checked_at TEXT,
    last_checked_at_iso TEXT,
    cert_days_left INTEGER,
    cert_not_after TEXT,
    last_down_notified_iso TEXT,
    last_cert_push_date TEXT,
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL,
    updated_at TEXT,
    updated_at_iso TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_health_checks_url ON checks(url);

CREATE TABLE IF NOT EXISTS disk_alerts (
    entity_id TEXT PRIMARY KEY,
    last_pct REAL,
    last_push_date TEXT
);

CREATE TABLE IF NOT EXISTS storage_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node TEXT NOT NULL,
    pool_key TEXT NOT NULL,
    used REAL,
    total REAL,
    pct REAL,
    sampled_at TEXT NOT NULL,
    sampled_at_iso TEXT NOT NULL,
    sampled_at_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_storage_samples_key
    ON storage_samples(node, pool_key, sampled_at_epoch);
"""


class HealthStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        await self._migrate()

    async def _migrate(self) -> None:
        db = self._require()
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS storage_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node TEXT NOT NULL,
                pool_key TEXT NOT NULL,
                used REAL,
                total REAL,
                pct REAL,
                sampled_at TEXT NOT NULL,
                sampled_at_iso TEXT NOT NULL,
                sampled_at_epoch REAL NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_storage_samples_key
            ON storage_samples(node, pool_key, sampled_at_epoch)
            """
        )
        await db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    def _require(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("HealthStore nicht verbunden")
        return self._db

    @staticmethod
    def _stamp() -> tuple[str, str]:
        now = now_berlin()
        return format_de(now), iso_utc(now)

    def _row(self, row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        d["enabled"] = bool(d.get("enabled"))
        return d

    async def list_checks(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        db = self._require()
        sql = "SELECT * FROM checks"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY label COLLATE NOCASE ASC"
        async with db.execute(sql) as cur:
            rows = await cur.fetchall()
        return [self._row(r) for r in rows]

    async def get_check(self, check_id: int) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute("SELECT * FROM checks WHERE id = ?", (check_id,)) as cur:
            row = await cur.fetchone()
        return self._row(row) if row else None

    async def find_by_url(self, url: str) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute("SELECT * FROM checks WHERE url = ?", (url,)) as cur:
            row = await cur.fetchone()
        return self._row(row) if row else None

    async def upsert_check(
        self,
        *,
        check_id: int | None,
        label: str,
        url: str,
        enabled: bool = True,
        source: str = "manual",
    ) -> int:
        db = self._require()
        stamp, stamp_iso = self._stamp()
        url = url.strip()
        label = label.strip() or url
        if check_id is not None:
            await db.execute(
                """
                UPDATE checks SET label=?, url=?, enabled=?,
                    updated_at=?, updated_at_iso=?
                WHERE id=?
                """,
                (label, url, 1 if enabled else 0, stamp, stamp_iso, check_id),
            )
            await db.commit()
            return int(check_id)
        existing = await self.find_by_url(url)
        if existing:
            await db.execute(
                """
                UPDATE checks SET label=?, enabled=?,
                    updated_at=?, updated_at_iso=?
                WHERE id=?
                """,
                (label, 1 if enabled else 0, stamp, stamp_iso, existing["id"]),
            )
            await db.commit()
            return int(existing["id"])
        cur = await db.execute(
            """
            INSERT INTO checks (
                label, url, enabled, source, created_at, created_at_iso,
                updated_at, updated_at_iso
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                label,
                url,
                1 if enabled else 0,
                source,
                stamp,
                stamp_iso,
                stamp,
                stamp_iso,
            ),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def delete_check(self, check_id: int) -> None:
        db = self._require()
        await db.execute("DELETE FROM checks WHERE id = ?", (check_id,))
        await db.commit()

    async def record_result(
        self,
        check_id: int,
        *,
        status: str,
        http_code: int | None = None,
        error: str | None = None,
        cert_days_left: int | None = None,
        cert_not_after: str | None = None,
    ) -> dict[str, Any]:
        db = self._require()
        stamp, stamp_iso = self._stamp()
        await db.execute(
            """
            UPDATE checks SET last_status=?, last_http_code=?, last_error=?,
                last_checked_at=?, last_checked_at_iso=?,
                cert_days_left=?, cert_not_after=?,
                updated_at=?, updated_at_iso=?
            WHERE id=?
            """,
            (
                status,
                http_code,
                error,
                stamp,
                stamp_iso,
                cert_days_left,
                cert_not_after,
                stamp,
                stamp_iso,
                check_id,
            ),
        )
        await db.commit()
        row = await self.get_check(check_id)
        assert row is not None
        return row

    async def mark_down_notified(self, check_id: int) -> None:
        db = self._require()
        _, stamp_iso = self._stamp()
        await db.execute(
            "UPDATE checks SET last_down_notified_iso=? WHERE id=?",
            (stamp_iso, check_id),
        )
        await db.commit()

    async def mark_cert_pushed(self, check_id: int, day: str) -> None:
        db = self._require()
        await db.execute(
            "UPDATE checks SET last_cert_push_date=? WHERE id=?",
            (day, check_id),
        )
        await db.commit()

    async def get_disk_alert(self, entity_id: str) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute(
            "SELECT * FROM disk_alerts WHERE entity_id = ?", (entity_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def set_disk_alert(
        self, entity_id: str, *, pct: float, push_date: str
    ) -> None:
        db = self._require()
        await db.execute(
            """
            INSERT INTO disk_alerts (entity_id, last_pct, last_push_date)
            VALUES (?, ?, ?)
            ON CONFLICT(entity_id) DO UPDATE SET
                last_pct = excluded.last_pct,
                last_push_date = excluded.last_push_date
            """,
            (entity_id, pct, push_date),
        )
        await db.commit()

    async def record_storage_sample(
        self,
        *,
        node: str,
        pool_key: str,
        used: float | None,
        total: float | None,
        pct: float | None,
    ) -> None:
        if used is None and pct is None:
            return
        db = self._require()
        stamp, stamp_iso = self._stamp()
        epoch = now_berlin().timestamp()
        await db.execute(
            """
            INSERT INTO storage_samples (
                node, pool_key, used, total, pct,
                sampled_at, sampled_at_iso, sampled_at_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (node, pool_key, used, total, pct, stamp, stamp_iso, epoch),
        )
        await db.commit()
        await self._downsample_pool(node, pool_key)

    async def _downsample_pool(self, node: str, pool_key: str) -> None:
        from app.core.storage_health import downsample_samples

        db = self._require()
        async with db.execute(
            """
            SELECT id, used, total, pct, sampled_at_epoch AS ts
            FROM storage_samples
            WHERE node = ? AND pool_key = ?
            ORDER BY sampled_at_epoch ASC
            """,
            (node, pool_key),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        kept = downsample_samples(rows)
        if len(kept) >= len(rows):
            return
        keep_ids = {int(r["id"]) for r in kept if r.get("id") is not None}
        drop = [int(r["id"]) for r in rows if int(r["id"]) not in keep_ids]
        if not drop:
            return
        q = ",".join("?" * len(drop))
        await db.execute(f"DELETE FROM storage_samples WHERE id IN ({q})", drop)
        await db.commit()

    async def list_storage_samples(
        self, node: str, pool_key: str, *, limit: int = 80
    ) -> list[dict[str, Any]]:
        db = self._require()
        async with db.execute(
            """
            SELECT used, total, pct, sampled_at_epoch AS ts, sampled_at
            FROM storage_samples
            WHERE node = ? AND pool_key = ?
            ORDER BY sampled_at_epoch ASC
            LIMIT ?
            """,
            (node, pool_key, max(2, min(limit, 120))),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def attach_projections(
        self, node: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        from app.core.storage_health import fill_projection

        out = dict(data)
        for group, name_key in (
            ("storage", "storage"),
            ("zfs", "name"),
            ("lvmthin", "name"),
        ):
            rows = list(out.get(group) or [])
            enriched = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                copy = dict(row)
                key = f"{group}:{copy.get(name_key) or ''}"
                samples = await self.list_storage_samples(node, key)
                used = copy.get("used") or copy.get("alloc")
                total = copy.get("total") or copy.get("size")
                proj = fill_projection(samples, used=used, total=total)
                if proj:
                    copy["projection"] = proj
                enriched.append(copy)
            out[group] = enriched
        return out

    async def record_health_snapshot(self, node: str, data: dict[str, Any]) -> None:
        for group, name_key, used_key, total_key in (
            ("storage", "storage", "used", "total"),
            ("zfs", "name", "alloc", "size"),
            ("lvmthin", "name", "used", "total"),
        ):
            for row in data.get(group) or []:
                if not isinstance(row, dict):
                    continue
                name = row.get(name_key)
                if not name:
                    continue
                await self.record_storage_sample(
                    node=node,
                    pool_key=f"{group}:{name}",
                    used=row.get(used_key),
                    total=row.get(total_key),
                    pct=row.get("pct"),
                )
