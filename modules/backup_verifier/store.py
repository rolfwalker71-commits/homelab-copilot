"""SQLite history for backup runs, restores, and schedules."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from app.core.locale import format_de, iso_utc, now_berlin
from backup_verifier.scheduler import schedule_start_sort_key

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
    dest_mode TEXT NOT NULL DEFAULT 'staging',
    dest_place TEXT NOT NULL DEFAULT 'copilot',
    scope TEXT NOT NULL DEFAULT 'stack',
    paths_json TEXT,
    staging_path TEXT,
    log_text TEXT,
    error_message TEXT,
    FOREIGN KEY (backup_run_id) REFERENCES backup_runs(id)
);

CREATE TABLE IF NOT EXISTS drill_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL,
    finished_at TEXT,
    finished_at_iso TEXT,
    status TEXT NOT NULL,
    engine TEXT NOT NULL DEFAULT '',
    target_key TEXT NOT NULL DEFAULT '',
    dest_label TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    duration_s REAL
);
CREATE INDEX IF NOT EXISTS idx_drill_runs_created ON drill_runs(created_at_iso DESC);

CREATE TABLE IF NOT EXISTS drill_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_fired_date TEXT NOT NULL DEFAULT '',
    last_status TEXT NOT NULL DEFAULT '',
    last_finished_at TEXT NOT NULL DEFAULT '',
    last_finished_at_iso TEXT NOT NULL DEFAULT '',
    last_summary_json TEXT,
    last_push_kind TEXT NOT NULL DEFAULT ''
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

CREATE TABLE IF NOT EXISTS restic_secrets (
    parent_id TEXT NOT NULL,
    project TEXT NOT NULL,
    password TEXT NOT NULL,
    last_full_at TEXT NOT NULL DEFAULT '',
    last_full_at_iso TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL,
    PRIMARY KEY (parent_id, project)
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
        if "engine" not in sched_cols:
            await db.execute(
                "ALTER TABLE schedules ADD COLUMN engine TEXT NOT NULL DEFAULT 'tar'"
            )
        if "restic_full_every_days" not in sched_cols:
            await db.execute(
                "ALTER TABLE schedules ADD COLUMN restic_full_every_days INTEGER NOT NULL DEFAULT 7"
            )
        if "restic_keep_last" not in sched_cols:
            await db.execute(
                "ALTER TABLE schedules ADD COLUMN restic_keep_last INTEGER NOT NULL DEFAULT 14"
            )
        if "restic_keep_weekly" not in sched_cols:
            await db.execute(
                "ALTER TABLE schedules ADD COLUMN restic_keep_weekly INTEGER NOT NULL DEFAULT 8"
            )
        if "engine" not in cols:
            await db.execute(
                "ALTER TABLE backup_runs ADD COLUMN engine TEXT NOT NULL DEFAULT 'tar'"
            )
        if "snapshot_id" not in cols:
            await db.execute("ALTER TABLE backup_runs ADD COLUMN snapshot_id TEXT")
        if "bytes_added" not in cols:
            await db.execute("ALTER TABLE backup_runs ADD COLUMN bytes_added INTEGER")
        if "bytes_processed" not in cols:
            await db.execute(
                "ALTER TABLE backup_runs ADD COLUMN bytes_processed INTEGER"
            )
        async with db.execute("PRAGMA table_info(restore_runs)") as cur:
            restore_cols = {row[1] for row in await cur.fetchall()}
        for col, spec in (
            ("dest_mode", "TEXT NOT NULL DEFAULT 'staging'"),
            ("dest_place", "TEXT NOT NULL DEFAULT 'copilot'"),
            ("scope", "TEXT NOT NULL DEFAULT 'stack'"),
            ("paths_json", "TEXT"),
            ("staging_path", "TEXT"),
        ):
            if col not in restore_cols:
                await db.execute(f"ALTER TABLE restore_runs ADD COLUMN {col} {spec}")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS drill_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                created_at_iso TEXT NOT NULL,
                finished_at TEXT,
                finished_at_iso TEXT,
                status TEXT NOT NULL,
                engine TEXT NOT NULL DEFAULT '',
                target_key TEXT NOT NULL DEFAULT '',
                dest_label TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                duration_s REAL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_drill_runs_created
            ON drill_runs(created_at_iso DESC)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS drill_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_fired_date TEXT NOT NULL DEFAULT '',
                last_status TEXT NOT NULL DEFAULT '',
                last_finished_at TEXT NOT NULL DEFAULT '',
                last_finished_at_iso TEXT NOT NULL DEFAULT '',
                last_summary_json TEXT,
                last_push_kind TEXT NOT NULL DEFAULT ''
            )
            """
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
        for key in ("preflight_json", "manifest_json", "destinations_json", "paths_json"):
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
        engine: str = "tar",
    ) -> int:
        db = self._require()
        now = now_berlin()
        eng = "restic" if str(engine).strip().lower() == "restic" else "tar"
        cur = await db.execute(
            """
            INSERT INTO backup_runs (
                stack, parent_id, guest_name, status, engine,
                created_at, created_at_iso, preflight_json,
                lxc_status, copilot_status, synology_status, verify_status
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, 'pending', 'pending', 'pending', 'pending')
            """,
            (
                stack,
                parent_id,
                guest_name,
                eng,
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
            "engine",
            "snapshot_id",
            "bytes_added",
            "bytes_processed",
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

    async def create_restore(
        self,
        *,
        backup_run_id: int,
        source: str,
        dest_mode: str = "staging",
        dest_place: str = "copilot",
        scope: str = "stack",
        paths: list[str] | None = None,
        staging_path: str | None = None,
    ) -> int:
        db = self._require()
        now = now_berlin()
        cur = await db.execute(
            """
            INSERT INTO restore_runs (
                backup_run_id, status, created_at, created_at_iso, source,
                dest_mode, dest_place, scope, paths_json, staging_path
            ) VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                backup_run_id,
                format_de(now),
                iso_utc(now),
                source,
                dest_mode,
                dest_place,
                scope,
                json.dumps(paths or [], ensure_ascii=False),
                staging_path or "",
            ),
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
            "dest_mode",
            "dest_place",
            "scope",
            "paths_json",
            "staging_path",
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
        return [self._row_to_dict(r) for r in rows]

    async def create_drill_run(
        self,
        *,
        engine: str,
        target_key: str,
        dest_label: str = "",
    ) -> int:
        db = self._require()
        now = now_berlin()
        cur = await db.execute(
            """
            INSERT INTO drill_runs (
                created_at, created_at_iso, status, engine, target_key, dest_label
            ) VALUES (?, ?, 'running', ?, ?, ?)
            """,
            (format_de(now), iso_utc(now), engine, target_key, dest_label),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def finish_drill_run(
        self,
        drill_id: int,
        *,
        status: str,
        detail: str = "",
        duration_s: float | None = None,
    ) -> None:
        db = self._require()
        now = now_berlin()
        await db.execute(
            """
            UPDATE drill_runs SET
                status = ?, detail = ?, duration_s = ?,
                finished_at = ?, finished_at_iso = ?
            WHERE id = ?
            """,
            (
                status,
                detail,
                duration_s,
                format_de(now),
                iso_utc(now),
                drill_id,
            ),
        )
        await db.commit()

    async def list_drill_runs(self, *, limit: int = 30) -> list[dict[str, Any]]:
        db = self._require()
        limit = max(1, min(limit, 100))
        async with db.execute(
            "SELECT * FROM drill_runs ORDER BY created_at_iso DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def latest_drill_summary(self) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute("SELECT * FROM drill_state WHERE id = 1") as cur:
            row = await cur.fetchone()
        if not row:
            return None
        data = dict(row)
        raw = data.get("last_summary_json")
        if isinstance(raw, str) and raw:
            try:
                data["summary"] = json.loads(raw)
            except json.JSONDecodeError:
                data["summary"] = None
        else:
            data["summary"] = None
        return data

    async def set_drill_state(
        self,
        *,
        fired_date: str,
        status: str,
        summary: dict[str, Any] | None = None,
        push_kind: str = "",
    ) -> None:
        db = self._require()
        now = now_berlin()
        await db.execute(
            """
            INSERT INTO drill_state (
                id, last_fired_date, last_status,
                last_finished_at, last_finished_at_iso,
                last_summary_json, last_push_kind
            ) VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_fired_date = excluded.last_fired_date,
                last_status = excluded.last_status,
                last_finished_at = excluded.last_finished_at,
                last_finished_at_iso = excluded.last_finished_at_iso,
                last_summary_json = excluded.last_summary_json,
                last_push_kind = excluded.last_push_kind
            """,
            (
                fired_date,
                status,
                format_de(now),
                iso_utc(now),
                json.dumps(summary or {}, ensure_ascii=False),
                push_kind,
            ),
        )
        await db.commit()

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
        out.sort(key=schedule_start_sort_key)
        return out

    async def find_schedules_for_stack(
        self, parent_id: str, stack: str
    ) -> list[dict[str, Any]]:
        """All schedules for the same parent + compose project (any order)."""
        parent_id = str(parent_id or "").strip()
        stack = str(stack or "").strip()
        if not parent_id or not stack:
            return []
        return [
            row
            for row in await self.list_schedules()
            if str(row.get("parent_id") or "") == parent_id
            and str(row.get("stack") or "") == stack
        ]

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
        engine: str = "tar",
        restic_full_every_days: int = 7,
        restic_keep_last: int = 14,
        restic_keep_weekly: int = 8,
    ) -> int:
        db = self._require()
        now = now_berlin()
        eng = "restic" if str(engine).strip().lower() == "restic" else "tar"
        full_days = max(1, min(int(restic_full_every_days or 7), 365))
        keep_last = max(1, min(int(restic_keep_last or 14), 365))
        keep_weekly = max(0, min(int(restic_keep_weekly or 8), 104))
        if schedule_id is not None:
            existing = await self.get_schedule(schedule_id)
            if not existing:
                raise ValueError(f"Zeitplan {schedule_id} nicht gefunden")
            await db.execute(
                """
                UPDATE schedules SET
                    stack = ?, parent_id = ?, cron_expr = ?, preset = ?,
                    enabled = ?, note = ?, engine = ?,
                    restic_full_every_days = ?, restic_keep_last = ?,
                    restic_keep_weekly = ?,
                    updated_at = ?, updated_at_iso = ?
                WHERE id = ?
                """,
                (
                    stack,
                    parent_id,
                    cron_expr,
                    preset,
                    1 if enabled else 0,
                    note,
                    eng,
                    full_days,
                    keep_last,
                    keep_weekly,
                    format_de(now),
                    iso_utc(now),
                    schedule_id,
                ),
            )
            await db.commit()
            return int(schedule_id)
        cur = await db.execute(
            """
            INSERT INTO schedules (
                stack, parent_id, cron_expr, preset, enabled,
                created_at, created_at_iso, note, engine,
                restic_full_every_days, restic_keep_last, restic_keep_weekly
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                eng,
                full_days,
                keep_last,
                keep_weekly,
            ),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def get_restic_password(self, parent_id: str, project: str) -> str | None:
        db = self._require()
        async with db.execute(
            "SELECT password FROM restic_secrets WHERE parent_id = ? AND project = ?",
            (parent_id, project),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        pw = (row["password"] or "").strip()
        return pw or None

    async def get_restic_secret_meta(
        self, parent_id: str, project: str
    ) -> dict[str, Any] | None:
        """Metadata only — never includes the password."""
        db = self._require()
        async with db.execute(
            """
            SELECT parent_id, project, last_full_at, last_full_at_iso, created_at
            FROM restic_secrets WHERE parent_id = ? AND project = ?
            """,
            (parent_id, project),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_or_create_restic_password(
        self, parent_id: str, project: str
    ) -> str:
        existing = await self.get_restic_password(parent_id, project)
        if existing:
            return existing
        import secrets

        password = secrets.token_urlsafe(32)
        db = self._require()
        now = now_berlin()
        await db.execute(
            """
            INSERT INTO restic_secrets (
                parent_id, project, password, created_at, created_at_iso
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (parent_id, project, password, format_de(now), iso_utc(now)),
        )
        await db.commit()
        return password

    async def mark_restic_full(self, parent_id: str, project: str) -> None:
        db = self._require()
        now = now_berlin()
        await db.execute(
            """
            UPDATE restic_secrets SET last_full_at = ?, last_full_at_iso = ?
            WHERE parent_id = ? AND project = ?
            """,
            (format_de(now), iso_utc(now), parent_id, project),
        )
        await db.commit()

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
