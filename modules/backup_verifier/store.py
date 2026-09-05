"""SQLite history for backup runs, restores, and schedules."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from app.core.locale import format_de, iso_utc, now_berlin

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backup_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stack TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    guest_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL,
    finished_at TEXT,
    finished_at_iso TEXT,
    size_bytes INTEGER,
    archive_sha256 TEXT,
    archive_name TEXT,
    lxc_path TEXT,
    lxc_status TEXT,
    lxc_verify TEXT,
    copilot_path TEXT,
    copilot_status TEXT,
    copilot_verify TEXT,
    synology_path TEXT,
    synology_status TEXT,
    synology_verify TEXT,
    verify_status TEXT,
    verify_detail TEXT,
    preflight_json TEXT,
    manifest_json TEXT,
    destinations_json TEXT,
    log_text TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_backup_runs_created ON backup_runs(created_at_iso DESC);
CREATE INDEX IF NOT EXISTS idx_backup_runs_stack ON backup_runs(stack, parent_id);

CREATE TABLE IF NOT EXISTS restore_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backup_run_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL,
    finished_at TEXT,
    finished_at_iso TEXT,
    source TEXT,
    log_text TEXT,
    error_message TEXT,
    FOREIGN KEY (backup_run_id) REFERENCES backup_runs(id)
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stack TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    cron_expr TEXT NOT NULL,
    preset TEXT NOT NULL DEFAULT 'custom',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL,
    updated_at TEXT,
    updated_at_iso TEXT,
    note TEXT DEFAULT '',
    last_fired_minute TEXT NOT NULL DEFAULT '',
    last_fired_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS backup_destinations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    preset TEXT NOT NULL DEFAULT 'custom',
    host TEXT NOT NULL DEFAULT '',
    port INTEGER NOT NULL DEFAULT 22,
    username TEXT NOT NULL DEFAULT '',
    remote_path TEXT NOT NULL DEFAULT '',
    auth_mode TEXT NOT NULL DEFAULT 'key_docker',
    secret_ref TEXT NOT NULL DEFAULT '',
    keep_count INTEGER NOT NULL DEFAULT 5,
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL,
    updated_at TEXT,
    updated_at_iso TEXT
);
CREATE INDEX IF NOT EXISTS idx_backup_destinations_order
    ON backup_destinations(sort_order ASC, id ASC);
"""


class BackupStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        db = self._require()
        async with db.execute("PRAGMA table_info(backup_runs)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "destinations_json" not in cols:
            await db.execute(
                "ALTER TABLE backup_runs ADD COLUMN destinations_json TEXT"
            )
        async with db.execute("PRAGMA table_info(schedules)") as cur:
            sched_cols = {row[1] for row in await cur.fetchall()}
        if "last_fired_minute" not in sched_cols:
            await db.execute(
                "ALTER TABLE schedules ADD COLUMN last_fired_minute TEXT NOT NULL DEFAULT ''"
            )
        if "last_fired_at" not in sched_cols:
            await db.execute(
                "ALTER TABLE schedules ADD COLUMN last_fired_at TEXT NOT NULL DEFAULT ''"
            )

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    def _require(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("BackupStore nicht verbunden")
        return self._db

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("preflight_json", "manifest_json", "destinations_json"):
            raw = data.get(key)
            short = key.replace("_json", "")
            if isinstance(raw, str) and raw:
                try:
                    data[short] = json.loads(raw)
                except json.JSONDecodeError:
                    data[short] = None
            else:
                data[short] = None if key.endswith("_json") else raw
        # Keep destinations_json raw for updates; also expose as destinations
        if data.get("destinations") is None and data.get("destinations_json"):
            pass
        return data

    async def list_destinations(self) -> list[dict[str, Any]]:
        db = self._require()
        async with db.execute(
            "SELECT * FROM backup_destinations ORDER BY sort_order ASC, id ASC"
        ) as cur:
            rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["enabled"] = bool(d.get("enabled"))
            out.append(d)
        return out

    async def replace_destinations(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace entire destination list (preserves ids when provided)."""
        db = self._require()
        now = now_berlin()
        stamp = format_de(now)
        stamp_iso = iso_utc(now)
        await db.execute("DELETE FROM backup_destinations")
        for i, item in enumerate(items):
            cols = (
                "sort_order, enabled, kind, label, preset, "
                "host, port, username, remote_path, "
                "auth_mode, secret_ref, keep_count, "
                "created_at, created_at_iso, updated_at, updated_at_iso"
            )
            vals: list[Any] = [
                int(item.get("sort_order") if item.get("sort_order") is not None else i),
                1 if item.get("enabled", True) else 0,
                item.get("kind"),
                item.get("label") or "",
                item.get("preset") or "custom",
                item.get("host") or "",
                int(item.get("port") or 22),
                item.get("username") or "",
                item.get("remote_path") or "",
                item.get("auth_mode") or "key_docker",
                item.get("secret_ref") or "",
                int(item.get("keep_count") if item.get("keep_count") is not None else 5),
                stamp,
                stamp_iso,
                stamp,
                stamp_iso,
            ]
            rid = item.get("id")
            if rid is not None:
                await db.execute(
                    f"""
                    INSERT INTO backup_destinations (
                        id, {cols}
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [int(rid), *vals],
                )
            else:
                await db.execute(
                    f"""
                    INSERT INTO backup_destinations (
                        {cols}
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    vals,
                )
        await db.commit()
        return await self.list_destinations()

    async def get_destination(self, dest_id: int) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute(
            "SELECT * FROM backup_destinations WHERE id = ?", (dest_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["enabled"] = bool(d.get("enabled"))
        return d

    async def create_run(
        self,
        *,
        stack: str,
        parent_id: str,
        guest_name: str,
        preflight: dict[str, Any] | None = None,
    ) -> int:
        db = self._require()
        now = now_berlin()
        cur = await db.execute(
            """
            INSERT INTO backup_runs (
                stack, parent_id, guest_name, status,
                created_at, created_at_iso, preflight_json,
                lxc_status, copilot_status, synology_status, verify_status
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, 'pending', 'pending', 'pending', 'pending')
            """,
            (
                stack,
                parent_id,
                guest_name,
                format_de(now),
                iso_utc(now),
                json.dumps(preflight or {}, ensure_ascii=False),
            ),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def update_run(self, run_id: int, **fields: Any) -> None:
        db = self._require()
        if not fields:
            return
        allowed = {
            "status",
            "finished_at",
            "finished_at_iso",
            "size_bytes",
            "archive_sha256",
            "archive_name",
            "lxc_path",
            "lxc_status",
            "lxc_verify",
            "copilot_path",
            "copilot_status",
            "copilot_verify",
            "synology_path",
            "synology_status",
            "synology_verify",
            "verify_status",
            "verify_detail",
            "preflight_json",
            "manifest_json",
            "destinations_json",
            "log_text",
            "error_message",
        }
        cols: list[str] = []
        vals: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key.endswith("_json") and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            cols.append(f"{key} = ?")
            vals.append(value)
        if not cols:
            return
        vals.append(run_id)
        await db.execute(f"UPDATE backup_runs SET {', '.join(cols)} WHERE id = ?", vals)
        await db.commit()

    async def append_log(self, run_id: int, line: str) -> None:
        db = self._require()
        async with db.execute(
            "SELECT log_text FROM backup_runs WHERE id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        prev = (row["log_text"] if row else None) or ""
        new = f"{prev}{line}\n" if prev else f"{line}\n"
        await db.execute(
            "UPDATE backup_runs SET log_text = ? WHERE id = ?", (new, run_id)
        )
        await db.commit()

    async def get_run(self, run_id: int) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute("SELECT * FROM backup_runs WHERE id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_runs(
        self, *, limit: int = 50, stack: str | None = None
    ) -> list[dict[str, Any]]:
        db = self._require()
        limit = max(1, min(limit, 200))
        if stack:
            sql = (
                "SELECT * FROM backup_runs WHERE stack = ? "
                "ORDER BY created_at_iso DESC LIMIT ?"
            )
            args: tuple[Any, ...] = (stack, limit)
        else:
            sql = "SELECT * FROM backup_runs ORDER BY created_at_iso DESC LIMIT ?"
            args = (limit,)
        async with db.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def create_restore(self, *, backup_run_id: int, source: str) -> int:
        db = self._require()
        now = now_berlin()
        cur = await db.execute(
            """
            INSERT INTO restore_runs (
                backup_run_id, status, created_at, created_at_iso, source
            ) VALUES (?, 'running', ?, ?, ?)
            """,
            (backup_run_id, format_de(now), iso_utc(now), source),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def update_restore(self, restore_id: int, **fields: Any) -> None:
        db = self._require()
        allowed = {
            "status",
            "finished_at",
            "finished_at_iso",
            "log_text",
            "error_message",
            "source",
        }
        cols: list[str] = []
        vals: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            cols.append(f"{key} = ?")
            vals.append(value)
        if not cols:
            return
        vals.append(restore_id)
        await db.execute(f"UPDATE restore_runs SET {', '.join(cols)} WHERE id = ?", vals)
        await db.commit()

    async def append_restore_log(self, restore_id: int, line: str) -> None:
        db = self._require()
        async with db.execute(
            "SELECT log_text FROM restore_runs WHERE id = ?", (restore_id,)
        ) as cur:
            row = await cur.fetchone()
        prev = (row["log_text"] if row else None) or ""
        new = f"{prev}{line}\n" if prev else f"{line}\n"
        await db.execute(
            "UPDATE restore_runs SET log_text = ? WHERE id = ?", (new, restore_id)
        )
        await db.commit()

    async def list_restores(self, *, limit: int = 30) -> list[dict[str, Any]]:
        db = self._require()
        limit = max(1, min(limit, 100))
        async with db.execute(
            "SELECT * FROM restore_runs ORDER BY created_at_iso DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_schedules(self) -> list[dict[str, Any]]:
        db = self._require()
        async with db.execute(
            "SELECT * FROM schedules ORDER BY id ASC"
        ) as cur:
            rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["enabled"] = bool(d.get("enabled"))
            out.append(d)
        return out

    async def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute(
            "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_schedule(
        self,
        *,
        schedule_id: int | None,
        stack: str,
        parent_id: str,
        cron_expr: str,
        preset: str,
        enabled: bool,
        note: str = "",
    ) -> int:
        db = self._require()
        now = now_berlin()
        if schedule_id:
            await db.execute(
                """
                UPDATE schedules SET
                    stack = ?, parent_id = ?, cron_expr = ?, preset = ?,
                    enabled = ?, note = ?, updated_at = ?, updated_at_iso = ?
                WHERE id = ?
                """,
                (
                    stack,
                    parent_id,
                    cron_expr,
                    preset,
                    1 if enabled else 0,
                    note,
                    format_de(now),
                    iso_utc(now),
                    schedule_id,
                ),
            )
            await db.commit()
            return schedule_id
        cur = await db.execute(
            """
            INSERT INTO schedules (
                stack, parent_id, cron_expr, preset, enabled,
                created_at, created_at_iso, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stack,
                parent_id,
                cron_expr,
                preset,
                1 if enabled else 0,
                format_de(now),
                iso_utc(now),
                note,
            ),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def delete_schedule(self, schedule_id: int) -> None:
        db = self._require()
        await db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        await db.commit()

    async def mark_schedule_fired(self, schedule_id: int, *, minute_key: str) -> None:
        db = self._require()
        now = now_berlin()
        await db.execute(
            """
            UPDATE schedules SET
                last_fired_minute = ?, last_fired_at = ?,
                updated_at = ?, updated_at_iso = ?
            WHERE id = ?
            """,
            (
                minute_key,
                format_de(now),
                format_de(now),
                iso_utc(now),
                schedule_id,
            ),
        )
        await db.commit()
