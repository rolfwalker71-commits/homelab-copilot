"""SQLite windows, shift history, and confirmation policy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from app.core.locale import format_de, iso_utc, now_berlin
from ops_agent.policy import ConfirmPolicy, default_policy, policy_from_row

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ops_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_name TEXT NOT NULL DEFAULT '',
    stack TEXT NOT NULL DEFAULT '',
    bucket TEXT NOT NULL DEFAULT '',
    start_iso TEXT NOT NULL,
    start_hm TEXT NOT NULL,
    duration_min INTEGER NOT NULL DEFAULT 10,
    status TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'agent',
    schedule_id INTEGER,
    job_id TEXT,
    needs_confirm INTEGER NOT NULL DEFAULT 0,
    confirm_reasons_json TEXT,
    gates_json TEXT,
    packages_json TEXT,
    reason TEXT NOT NULL DEFAULT '',
    extra_json TEXT,
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL,
    updated_at TEXT,
    updated_at_iso TEXT
);
CREATE INDEX IF NOT EXISTS idx_ops_windows_start ON ops_windows(start_iso);
CREATE INDEX IF NOT EXISTS idx_ops_windows_status ON ops_windows(status);
CREATE INDEX IF NOT EXISTS idx_ops_windows_sched ON ops_windows(schedule_id);

CREATE TABLE IF NOT EXISTS ops_shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL,
    old_start_iso TEXT NOT NULL,
    old_start_hm TEXT NOT NULL,
    new_start_iso TEXT NOT NULL,
    new_start_hm TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ops_shifts_created ON ops_shifts(created_at_iso DESC);

CREATE TABLE IF NOT EXISTS ops_policy (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    answered INTEGER NOT NULL DEFAULT 0,
    confirm_kernel_docker INTEGER NOT NULL DEFAULT 1,
    confirm_new_guest_backup INTEGER NOT NULL DEFAULT 1,
    confirm_production INTEGER NOT NULL DEFAULT 0,
    confirm_nothing INTEGER NOT NULL DEFAULT 0,
    production_tags TEXT NOT NULL DEFAULT '["prod","production"]',
    focus_mode TEXT NOT NULL DEFAULT 'all',
    focus_ids TEXT NOT NULL DEFAULT '[]',
    focus_tags TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT,
    updated_at_iso TEXT
);

CREATE TABLE IF NOT EXISTS ops_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER,
    shift_auto INTEGER,
    quiet_start TEXT,
    quiet_end TEXT,
    patch_halted INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    updated_at_iso TEXT
);
"""


def _json_list(raw: Any) -> list[Any]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return raw
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _json_obj(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


class OpsStore:
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
            raise RuntimeError("Ops-Agent-Store nicht verbunden")
        return self._db

    def _window_from_row(self, row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        d["needs_confirm"] = bool(d.get("needs_confirm"))
        d["confirm_reasons"] = _json_list(d.pop("confirm_reasons_json", None))
        d["gates"] = _json_list(d.pop("gates_json", None))
        d["packages"] = _json_list(d.pop("packages_json", None))
        extra = _json_obj(d.pop("extra_json", None))
        d["extra"] = extra
        d["engine"] = extra.get("engine") or "tar"
        d["tags"] = extra.get("tags") or []
        return d

    async def get_policy(self) -> ConfirmPolicy:
        db = self._require()
        async with db.execute("SELECT * FROM ops_policy WHERE id = 1") as cur:
            row = await cur.fetchone()
        if not row:
            return default_policy()
        d = dict(row)
        d["answered"] = bool(d.get("answered"))
        d["confirm_kernel_docker"] = bool(d.get("confirm_kernel_docker"))
        d["confirm_new_guest_backup"] = bool(d.get("confirm_new_guest_backup"))
        d["confirm_production"] = bool(d.get("confirm_production"))
        d["confirm_nothing"] = bool(d.get("confirm_nothing"))
        d["production_tags"] = _json_list(d.get("production_tags"))
        d["focus_ids"] = _json_list(d.get("focus_ids"))
        d["focus_tags"] = _json_list(d.get("focus_tags"))
        return policy_from_row(d)

    async def save_policy(self, policy: ConfirmPolicy) -> ConfirmPolicy:
        db = self._require()
        now = now_berlin()
        await db.execute(
            """
            INSERT INTO ops_policy (
                id, answered, confirm_kernel_docker, confirm_new_guest_backup,
                confirm_production, confirm_nothing, production_tags,
                focus_mode, focus_ids, focus_tags, updated_at, updated_at_iso
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                answered = excluded.answered,
                confirm_kernel_docker = excluded.confirm_kernel_docker,
                confirm_new_guest_backup = excluded.confirm_new_guest_backup,
                confirm_production = excluded.confirm_production,
                confirm_nothing = excluded.confirm_nothing,
                production_tags = excluded.production_tags,
                focus_mode = excluded.focus_mode,
                focus_ids = excluded.focus_ids,
                focus_tags = excluded.focus_tags,
                updated_at = excluded.updated_at,
                updated_at_iso = excluded.updated_at_iso
            """,
            (
                1 if policy.answered else 0,
                1 if policy.confirm_kernel_docker else 0,
                1 if policy.confirm_new_guest_backup else 0,
                1 if policy.confirm_production else 0,
                1 if policy.confirm_nothing else 0,
                json.dumps(policy.production_tags, ensure_ascii=False),
                policy.focus_mode,
                json.dumps(policy.focus_ids, ensure_ascii=False),
                json.dumps(policy.focus_tags, ensure_ascii=False),
                format_de(now),
                iso_utc(now),
            ),
        )
        await db.commit()
        return await self.get_policy()

    async def get_settings(self) -> dict[str, Any]:
        db = self._require()
        async with db.execute("SELECT * FROM ops_settings WHERE id = 1") as cur:
            row = await cur.fetchone()
        if not row:
            return {
                "enabled": None,
                "shift_auto": None,
                "quiet_start": None,
                "quiet_end": None,
                "patch_halted": False,
            }
        d = dict(row)
        return {
            "enabled": None if d.get("enabled") is None else bool(d.get("enabled")),
            "shift_auto": None
            if d.get("shift_auto") is None
            else bool(d.get("shift_auto")),
            "quiet_start": d.get("quiet_start"),
            "quiet_end": d.get("quiet_end"),
            "patch_halted": bool(d.get("patch_halted")),
        }

    async def save_settings(
        self,
        *,
        enabled: bool | None = None,
        shift_auto: bool | None = None,
        quiet_start: str | None = None,
        quiet_end: str | None = None,
        patch_halted: bool | None = None,
    ) -> dict[str, Any]:
        current = await self.get_settings()
        nxt = {
            "enabled": current["enabled"] if enabled is None else bool(enabled),
            "shift_auto": current["shift_auto"]
            if shift_auto is None
            else bool(shift_auto),
            "quiet_start": current["quiet_start"]
            if quiet_start is None
            else quiet_start,
            "quiet_end": current["quiet_end"] if quiet_end is None else quiet_end,
            "patch_halted": current["patch_halted"]
            if patch_halted is None
            else bool(patch_halted),
        }
        db = self._require()
        now = now_berlin()
        await db.execute(
            """
            INSERT INTO ops_settings (
                id, enabled, shift_auto, quiet_start, quiet_end,
                patch_halted, updated_at, updated_at_iso
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                enabled = excluded.enabled,
                shift_auto = excluded.shift_auto,
                quiet_start = excluded.quiet_start,
                quiet_end = excluded.quiet_end,
                patch_halted = excluded.patch_halted,
                updated_at = excluded.updated_at,
                updated_at_iso = excluded.updated_at_iso
            """,
            (
                None if nxt["enabled"] is None else (1 if nxt["enabled"] else 0),
                None if nxt["shift_auto"] is None else (1 if nxt["shift_auto"] else 0),
                nxt["quiet_start"],
                nxt["quiet_end"],
                1 if nxt["patch_halted"] else 0,
                format_de(now),
                iso_utc(now),
            ),
        )
        await db.commit()
        return await self.get_settings()

    async def list_windows(
        self,
        *,
        statuses: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        db = self._require()
        limit = max(1, min(int(limit), 500))
        if statuses:
            marks = ",".join("?" * len(statuses))
            sql = (
                f"SELECT * FROM ops_windows WHERE status IN ({marks}) "
                "ORDER BY start_iso ASC, id ASC LIMIT ?"
            )
            args: tuple[Any, ...] = (*statuses, limit)
        else:
            sql = "SELECT * FROM ops_windows ORDER BY start_iso ASC, id ASC LIMIT ?"
            args = (limit,)
        async with db.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [self._window_from_row(r) for r in rows]

    async def get_window(self, window_id: int) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute(
            "SELECT * FROM ops_windows WHERE id = ?", (window_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._window_from_row(row) if row else None

    async def find_by_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute(
            "SELECT * FROM ops_windows WHERE schedule_id = ? ORDER BY id DESC LIMIT 1",
            (schedule_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._window_from_row(row) if row else None

    async def find_open_for_target(
        self, *, kind: str, target_id: str, stack: str = ""
    ) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute(
            """
            SELECT * FROM ops_windows
            WHERE kind = ? AND target_id = ? AND stack = ?
              AND status IN ('accepted', 'waiting_confirm', 'running')
            ORDER BY start_iso ASC LIMIT 1
            """,
            (kind, target_id, stack),
        ) as cur:
            row = await cur.fetchone()
        return self._window_from_row(row) if row else None

    async def insert_window(self, row: dict[str, Any]) -> int:
        db = self._require()
        now = now_berlin()
        extra = dict(row.get("extra") or {})
        if row.get("engine"):
            extra["engine"] = row["engine"]
        if row.get("tags") is not None:
            extra["tags"] = row["tags"]
        cur = await db.execute(
            """
            INSERT INTO ops_windows (
                kind, target_id, target_name, stack, bucket,
                start_iso, start_hm, duration_min, status, source,
                schedule_id, job_id, needs_confirm, confirm_reasons_json,
                gates_json, packages_json, reason, extra_json,
                created_at, created_at_iso, updated_at, updated_at_iso
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["kind"],
                row["target_id"],
                row.get("target_name") or "",
                row.get("stack") or "",
                row.get("bucket") or "",
                row["start_iso"],
                row.get("start_hm") or "",
                int(row.get("duration_min") or 10),
                row.get("status") or "accepted",
                row.get("source") or "agent",
                row.get("schedule_id"),
                row.get("job_id"),
                1 if row.get("needs_confirm") else 0,
                json.dumps(row.get("confirm_reasons") or [], ensure_ascii=False),
                json.dumps(row.get("gates") or [], ensure_ascii=False),
                json.dumps(row.get("packages") or [], ensure_ascii=False),
                row.get("reason") or "",
                json.dumps(extra, ensure_ascii=False),
                format_de(now),
                iso_utc(now),
                format_de(now),
                iso_utc(now),
            ),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def update_window(self, window_id: int, **fields: Any) -> None:
        allowed = {
            "start_iso",
            "start_hm",
            "duration_min",
            "status",
            "schedule_id",
            "job_id",
            "needs_confirm",
            "reason",
            "gates",
            "confirm_reasons",
            "packages",
            "target_name",
        }
        sets: list[str] = []
        args: list[Any] = []
        for key, value in fields.items():
            if key not in allowed and key not in ("gates", "confirm_reasons", "packages"):
                continue
            col = key
            if key == "gates":
                col = "gates_json"
                value = json.dumps(value or [], ensure_ascii=False)
            elif key == "confirm_reasons":
                col = "confirm_reasons_json"
                value = json.dumps(value or [], ensure_ascii=False)
            elif key == "packages":
                col = "packages_json"
                value = json.dumps(value or [], ensure_ascii=False)
            elif key == "needs_confirm":
                value = 1 if value else 0
            sets.append(f"{col} = ?")
            args.append(value)
        if not sets:
            return
        now = now_berlin()
        sets.append("updated_at = ?")
        sets.append("updated_at_iso = ?")
        args.extend([format_de(now), iso_utc(now), window_id])
        db = self._require()
        await db.execute(
            f"UPDATE ops_windows SET {', '.join(sets)} WHERE id = ?",
            args,
        )
        await db.commit()

    async def add_shift(
        self,
        window_id: int,
        *,
        old_start_iso: str,
        old_start_hm: str,
        new_start_iso: str,
        new_start_hm: str,
        reason: str,
    ) -> int:
        db = self._require()
        now = now_berlin()
        cur = await db.execute(
            """
            INSERT INTO ops_shifts (
                window_id, old_start_iso, old_start_hm,
                new_start_iso, new_start_hm, reason,
                created_at, created_at_iso
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                window_id,
                old_start_iso,
                old_start_hm,
                new_start_iso,
                new_start_hm,
                reason,
                format_de(now),
                iso_utc(now),
            ),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def list_shifts(self, *, limit: int = 40) -> list[dict[str, Any]]:
        db = self._require()
        limit = max(1, min(int(limit), 200))
        async with db.execute(
            """
            SELECT s.*, w.kind, w.target_id, w.target_name, w.stack
            FROM ops_shifts s
            LEFT JOIN ops_windows w ON w.id = s.window_id
            ORDER BY s.created_at_iso DESC LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_skipped_for(self, *, kind: str, target_id: str, stack: str = "") -> None:
        db = self._require()
        await db.execute(
            """
            DELETE FROM ops_windows
            WHERE kind = ? AND target_id = ? AND stack = ? AND status = 'skipped'
              AND source = 'agent'
            """,
            (kind, target_id, stack),
        )
        await db.commit()
