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
    patch_scope_ids TEXT NOT NULL DEFAULT '[]',
    image_scope_ids TEXT NOT NULL DEFAULT '[]',
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

CREATE TABLE IF NOT EXISTS ops_known_hosts (
    target_id TEXT PRIMARY KEY,
    target_name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    gone INTEGER NOT NULL DEFAULT 0,
    keep_preference INTEGER NOT NULL DEFAULT 0,
    first_seen_iso TEXT,
    last_seen_iso TEXT
);

CREATE TABLE IF NOT EXISTS ops_scope_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id TEXT NOT NULL,
    target_name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',
    patch INTEGER,
    image INTEGER,
    drop_from_scope INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT,
    created_at_iso TEXT,
    answered_at TEXT,
    answered_at_iso TEXT
);
CREATE INDEX IF NOT EXISTS idx_ops_scope_prompts_status
    ON ops_scope_prompts(status, kind);

CREATE TABLE IF NOT EXISTS ops_image_snap (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    target_id TEXT,
    snap_name TEXT
);

CREATE TABLE IF NOT EXISTS ops_rollbacks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER,
    job_id TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL,
    target_name TEXT NOT NULL DEFAULT '',
    job_kind TEXT NOT NULL DEFAULT '',
    snap_name TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT 'Agent',
    via_agent INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ops_rollbacks_job ON ops_rollbacks(job_id);
CREATE INDEX IF NOT EXISTS idx_ops_rollbacks_window ON ops_rollbacks(window_id);
CREATE INDEX IF NOT EXISTS idx_ops_rollbacks_created ON ops_rollbacks(created_at_iso DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_rollbacks_job_uniq
    ON ops_rollbacks(job_id) WHERE job_id != '';
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
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        db = self._require()
        async with db.execute("PRAGMA table_info(ops_policy)") as cur:
            policy_cols = {row[1] for row in await cur.fetchall()}
        if "patch_scope_ids" not in policy_cols:
            await db.execute(
                "ALTER TABLE ops_policy ADD COLUMN patch_scope_ids TEXT NOT NULL DEFAULT '[]'"
            )
        if "image_scope_ids" not in policy_cols:
            await db.execute(
                "ALTER TABLE ops_policy ADD COLUMN image_scope_ids TEXT NOT NULL DEFAULT '[]'"
            )
        await self._seed_scopes_from_legacy_focus()
        async with db.execute("PRAGMA table_info(ops_rollbacks)") as cur:
            rb_cols = {row[1] for row in await cur.fetchall()}
        if rb_cols:
            if "actor" not in rb_cols:
                await db.execute(
                    "ALTER TABLE ops_rollbacks ADD COLUMN actor TEXT NOT NULL DEFAULT 'Agent'"
                )
            if "via_agent" not in rb_cols:
                await db.execute(
                    "ALTER TABLE ops_rollbacks ADD COLUMN via_agent INTEGER NOT NULL DEFAULT 1"
                )

    async def _seed_scopes_from_legacy_focus(self) -> None:
        """One-time: copy 'only these IDs' into both matrices. Never imply 'all'."""
        db = self._require()
        async with db.execute("SELECT * FROM ops_policy WHERE id = 1") as cur:
            row = await cur.fetchone()
        if not row:
            return
        d = dict(row)
        patch = _json_list(d.get("patch_scope_ids"))
        image = _json_list(d.get("image_scope_ids"))
        if patch or image:
            return
        if str(d.get("focus_mode") or "") != "only":
            return
        ids = _json_list(d.get("focus_ids"))
        if not ids:
            return
        payload = json.dumps(ids, ensure_ascii=False)
        await db.execute(
            "UPDATE ops_policy SET patch_scope_ids = ?, image_scope_ids = ? WHERE id = 1",
            (payload, payload),
        )

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
        d["patch_scope_ids"] = _json_list(d.get("patch_scope_ids"))
        d["image_scope_ids"] = _json_list(d.get("image_scope_ids"))
        return policy_from_row(d)

    async def save_policy(self, policy: ConfirmPolicy) -> ConfirmPolicy:
        db = self._require()
        now = now_berlin()
        await db.execute(
            """
            INSERT INTO ops_policy (
                id, answered, confirm_kernel_docker, confirm_new_guest_backup,
                confirm_production, confirm_nothing, production_tags,
                focus_mode, focus_ids, focus_tags, patch_scope_ids, image_scope_ids,
                updated_at, updated_at_iso
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                patch_scope_ids = excluded.patch_scope_ids,
                image_scope_ids = excluded.image_scope_ids,
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
                json.dumps(policy.patch_scope_ids, ensure_ascii=False),
                json.dumps(policy.image_scope_ids, ensure_ascii=False),
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

    def _known_from_row(self, row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        d["gone"] = bool(d.get("gone"))
        d["keep_preference"] = bool(d.get("keep_preference"))
        return d

    async def list_known_hosts(self) -> list[dict[str, Any]]:
        db = self._require()
        async with db.execute(
            "SELECT * FROM ops_known_hosts ORDER BY target_name COLLATE NOCASE, target_id"
        ) as cur:
            rows = await cur.fetchall()
        return [self._known_from_row(r) for r in rows]

    async def upsert_known_host(
        self,
        *,
        target_id: str,
        target_name: str = "",
        kind: str = "",
        gone: bool = False,
        keep_preference: bool | None = None,
    ) -> None:
        db = self._require()
        now = now_berlin()
        iso = iso_utc(now)
        keep_sql = ""
        keep_val: int | None = None
        if keep_preference is not None:
            keep_sql = ", keep_preference = excluded.keep_preference"
            keep_val = 1 if keep_preference else 0
        await db.execute(
            f"""
            INSERT INTO ops_known_hosts (
                target_id, target_name, kind, gone, keep_preference,
                first_seen_iso, last_seen_iso
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_id) DO UPDATE SET
                target_name = excluded.target_name,
                kind = excluded.kind,
                gone = excluded.gone,
                last_seen_iso = excluded.last_seen_iso
                {keep_sql}
            """,
            (
                target_id,
                target_name,
                kind,
                1 if gone else 0,
                keep_val if keep_val is not None else 0,
                iso,
                iso,
            ),
        )
        await db.commit()

    async def seed_known_hosts(self, hosts: list[dict[str, Any]]) -> None:
        db = self._require()
        now = now_berlin()
        iso = iso_utc(now)
        for h in hosts:
            tid = str(h.get("id") or h.get("target_id") or "").strip()
            if not tid:
                continue
            await db.execute(
                """
                INSERT OR IGNORE INTO ops_known_hosts (
                    target_id, target_name, kind, gone, keep_preference,
                    first_seen_iso, last_seen_iso
                ) VALUES (?, ?, ?, 0, 0, ?, ?)
                """,
                (
                    tid,
                    str(h.get("name") or tid),
                    str(h.get("kind") or ""),
                    iso,
                    iso,
                ),
            )
        await db.commit()

    async def mark_known_gone(self, target_id: str) -> None:
        db = self._require()
        await db.execute(
            "UPDATE ops_known_hosts SET gone = 1 WHERE target_id = ?",
            (target_id,),
        )
        await db.commit()

    async def mark_known_present(
        self, target_id: str, *, target_name: str = "", kind: str = ""
    ) -> None:
        db = self._require()
        now = now_berlin()
        sets = ["gone = 0", "last_seen_iso = ?"]
        args: list[Any] = [iso_utc(now)]
        if target_name:
            sets.append("target_name = ?")
            args.append(target_name)
        if kind:
            sets.append("kind = ?")
            args.append(kind)
        args.append(target_id)
        await db.execute(
            f"UPDATE ops_known_hosts SET {', '.join(sets)} WHERE target_id = ?",
            args,
        )
        await db.commit()

    async def delete_known_host(self, target_id: str) -> None:
        db = self._require()
        await db.execute("DELETE FROM ops_known_hosts WHERE target_id = ?", (target_id,))
        await db.commit()

    def _prompt_from_row(self, row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        d["patch"] = None if d.get("patch") is None else bool(d.get("patch"))
        d["image"] = None if d.get("image") is None else bool(d.get("image"))
        d["drop_from_scope"] = (
            None if d.get("drop_from_scope") is None else bool(d.get("drop_from_scope"))
        )
        return d

    async def list_scope_prompts(self, *, status: str | None = "waiting") -> list[dict[str, Any]]:
        db = self._require()
        if status:
            sql = (
                "SELECT * FROM ops_scope_prompts WHERE status = ? "
                "ORDER BY created_at_iso ASC, id ASC"
            )
            args: tuple[Any, ...] = (status,)
        else:
            sql = "SELECT * FROM ops_scope_prompts ORDER BY created_at_iso ASC, id ASC"
            args = ()
        async with db.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [self._prompt_from_row(r) for r in rows]

    async def find_waiting_prompt(self, target_id: str) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute(
            """
            SELECT * FROM ops_scope_prompts
            WHERE target_id = ? AND status = 'waiting'
            ORDER BY id DESC LIMIT 1
            """,
            (target_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._prompt_from_row(row) if row else None

    async def get_scope_prompt(self, prompt_id: int) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute(
            "SELECT * FROM ops_scope_prompts WHERE id = ?", (prompt_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._prompt_from_row(row) if row else None

    async def insert_scope_prompt(
        self,
        *,
        target_id: str,
        target_name: str,
        kind: str,
        reason: str,
    ) -> int | None:
        existing = await self.find_waiting_prompt(target_id)
        if existing:
            return int(existing["id"])
        db = self._require()
        now = now_berlin()
        cur = await db.execute(
            """
            INSERT INTO ops_scope_prompts (
                target_id, target_name, kind, status, reason,
                created_at, created_at_iso
            ) VALUES (?, ?, ?, 'waiting', ?, ?, ?)
            """,
            (
                target_id,
                target_name,
                kind,
                reason,
                format_de(now),
                iso_utc(now),
            ),
        )
        await db.commit()
        return int(cur.lastrowid) if cur.lastrowid else None

    async def answer_scope_prompt(
        self,
        prompt_id: int,
        *,
        patch: bool | None = None,
        image: bool | None = None,
        drop_from_scope: bool | None = None,
    ) -> dict[str, Any] | None:
        db = self._require()
        now = now_berlin()
        await db.execute(
            """
            UPDATE ops_scope_prompts SET
                status = 'answered',
                patch = ?,
                image = ?,
                drop_from_scope = ?,
                answered_at = ?,
                answered_at_iso = ?
            WHERE id = ?
            """,
            (
                None if patch is None else (1 if patch else 0),
                None if image is None else (1 if image else 0),
                None if drop_from_scope is None else (1 if drop_from_scope else 0),
                format_de(now),
                iso_utc(now),
                prompt_id,
            ),
        )
        await db.commit()
        return await self.get_scope_prompt(prompt_id)

    async def dismiss_scope_prompt(self, prompt_id: int, *, reason: str = "") -> None:
        db = self._require()
        now = now_berlin()
        await db.execute(
            """
            UPDATE ops_scope_prompts SET
                status = 'answered',
                reason = CASE WHEN ? != '' THEN ? ELSE reason END,
                answered_at = ?,
                answered_at_iso = ?
            WHERE id = ?
            """,
            (reason, reason, format_de(now), iso_utc(now), prompt_id),
        )
        await db.commit()

    async def get_ok_image_snap(self) -> dict[str, str] | None:
        db = self._require()
        async with db.execute("SELECT * FROM ops_image_snap WHERE id = 1") as cur:
            row = await cur.fetchone()
        if not row:
            return None
        tid = str(row["target_id"] or "").strip()
        name = str(row["snap_name"] or "").strip()
        if not tid or not name:
            return None
        return {"target_id": tid, "snap_name": name}

    async def set_ok_image_snap(self, target_id: str, snap_name: str) -> None:
        db = self._require()
        await db.execute(
            """
            INSERT INTO ops_image_snap (id, target_id, snap_name)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                target_id = excluded.target_id,
                snap_name = excluded.snap_name
            """,
            (target_id, snap_name),
        )
        await db.commit()

    async def clear_ok_image_snap(self) -> None:
        db = self._require()
        await db.execute("DELETE FROM ops_image_snap WHERE id = 1")
        await db.commit()

    def _rollback_from_row(self, row: aiosqlite.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        d["via_agent"] = bool(d.get("via_agent", 1))
        d["actor"] = str(d.get("actor") or "Agent")
        return d

    async def get_rollback_for_job(self, job_id: str) -> dict[str, Any] | None:
        jid = str(job_id or "").strip()
        if not jid:
            return None
        db = self._require()
        async with db.execute(
            """
            SELECT * FROM ops_rollbacks
            WHERE job_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (jid,),
        ) as cur:
            row = await cur.fetchone()
        return self._rollback_from_row(row)

    async def get_rollback_for_window(self, window_id: int) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute(
            """
            SELECT * FROM ops_rollbacks
            WHERE window_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (int(window_id),),
        ) as cur:
            row = await cur.fetchone()
        return self._rollback_from_row(row)

    async def list_rollbacks(self, *, limit: int = 40) -> list[dict[str, Any]]:
        db = self._require()
        limit = max(1, min(int(limit), 200))
        async with db.execute(
            """
            SELECT * FROM ops_rollbacks
            ORDER BY created_at_iso DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def insert_rollback(
        self,
        *,
        window_id: int | None,
        job_id: str,
        target_id: str,
        target_name: str,
        job_kind: str,
        snap_name: str,
        reason: str,
        status: str,
        error: str = "",
    ) -> dict[str, Any]:
        existing = await self.get_rollback_for_job(job_id)
        if existing:
            return existing
        db = self._require()
        now = now_berlin()
        cur = await db.execute(
            """
            INSERT INTO ops_rollbacks (
                window_id, job_id, target_id, target_name, job_kind,
                snap_name, reason, status, error, actor, via_agent,
                created_at, created_at_iso
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Agent', 1, ?, ?)
            """,
            (
                int(window_id) if window_id is not None else None,
                str(job_id or ""),
                str(target_id or ""),
                str(target_name or ""),
                str(job_kind or ""),
                str(snap_name or ""),
                str(reason or ""),
                str(status or ""),
                str(error or ""),
                format_de(now),
                iso_utc(now),
            ),
        )
        await db.commit()
        rid = int(cur.lastrowid) if cur.lastrowid else 0
        if rid:
            async with db.execute(
                "SELECT * FROM ops_rollbacks WHERE id = ?", (rid,)
            ) as sel:
                row = await sel.fetchone()
            found = self._rollback_from_row(row)
            if found:
                return found
        return {
            "id": rid,
            "window_id": window_id,
            "job_id": job_id,
            "target_id": target_id,
            "target_name": target_name,
            "job_kind": job_kind,
            "snap_name": snap_name,
            "reason": reason,
            "status": status,
            "error": error,
            "actor": "Agent",
            "via_agent": True,
        }
