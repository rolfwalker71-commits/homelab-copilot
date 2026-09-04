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
    note TEXT DEFAULT ''
);
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
        await self._db.commit()

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
        for key in ("preflight_json", "manifest_json"):
            raw = data.get(key)
            if isinstance(raw, str) and raw:
                try:
                    data[key.replace("_json", "")] = json.loads(raw)
                except json.JSONDecodeError:
                    data[key.replace("_json", "")] = None
            else:
                data[key.replace("_json", "")] = None
        return data

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
        return [dict(r) for r in rows]

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
