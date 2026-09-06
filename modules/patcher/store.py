"""SQLite store for patcher hosts, scans, packages, apply runs, schedules."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from app.core.locale import format_de, iso_utc, now_berlin

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 22,
    ssh_user TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL,
    updated_at TEXT,
    updated_at_iso TEXT
);
CREATE INDEX IF NOT EXISTS idx_patcher_hosts_host ON hosts(host);

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id TEXT NOT NULL,
    target_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    pm TEXT,
    distro TEXT,
    summary_json TEXT,
    llm_summary TEXT,
    reboot_required INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL,
    finished_at TEXT,
    finished_at_iso TEXT
);
CREATE INDEX IF NOT EXISTS idx_patcher_scans_target ON scans(target_id, created_at_iso DESC);
CREATE INDEX IF NOT EXISTS idx_patcher_scans_created ON scans(created_at_iso DESC);

CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    current_version TEXT,
    candidate_version TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',
    meta_json TEXT,
    FOREIGN KEY (scan_id) REFERENCES scans(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_patcher_packages_scan ON packages(scan_id);

CREATE TABLE IF NOT EXISTS apply_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id TEXT NOT NULL,
    target_name TEXT NOT NULL DEFAULT '',
    package_filter TEXT NOT NULL,
    packages_json TEXT,
    status TEXT NOT NULL,
    pm TEXT,
    log_text TEXT,
    reboot_required INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL,
    finished_at TEXT,
    finished_at_iso TEXT
);
CREATE INDEX IF NOT EXISTS idx_patcher_apply_created ON apply_runs(created_at_iso DESC);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id TEXT NOT NULL,
    cron_expr TEXT NOT NULL,
    preset TEXT NOT NULL DEFAULT 'daily',
    enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL,
    updated_at TEXT,
    updated_at_iso TEXT
);

CREATE TABLE IF NOT EXISTS target_prefs (
    target_id TEXT PRIMARY KEY,
    monitored INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT,
    updated_at_iso TEXT
);

CREATE TABLE IF NOT EXISTS image_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id TEXT NOT NULL,
    target_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    update_count INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL,
    finished_at TEXT,
    finished_at_iso TEXT
);
CREATE INDEX IF NOT EXISTS idx_patcher_image_scans_target
    ON image_scans(target_id, created_at_iso DESC);

CREATE TABLE IF NOT EXISTS agent_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER,
    auto_security INTEGER,
    max_parallel INTEGER,
    updated_at TEXT,
    updated_at_iso TEXT
);

CREATE TABLE IF NOT EXISTS waves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    stop_reason TEXT,
    failed_item_id INTEGER,
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL,
    updated_at TEXT,
    updated_at_iso TEXT,
    started_at TEXT,
    started_at_iso TEXT,
    finished_at TEXT,
    finished_at_iso TEXT
);

CREATE TABLE IF NOT EXISTS wave_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wave_id INTEGER NOT NULL,
    target_id TEXT NOT NULL,
    target_name TEXT NOT NULL DEFAULT '',
    bucket TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    needs_confirm INTEGER NOT NULL DEFAULT 1,
    confirmed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    package_filter TEXT NOT NULL,
    packages_json TEXT,
    reason TEXT DEFAULT '',
    confirm_reasons_json TEXT,
    gate_json TEXT,
    job_id TEXT,
    error_message TEXT,
    explanation TEXT,
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL,
    FOREIGN KEY (wave_id) REFERENCES waves(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_patcher_wave_items
    ON wave_items(wave_id, sort_order);
"""


class PatcherStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    def _require(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("PatcherStore nicht verbunden")
        return self._db

    @staticmethod
    def _stamp() -> tuple[str, str]:
        now = now_berlin()
        return format_de(now), iso_utc(now)

    # --- hosts ---

    async def list_hosts(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        db = self._require()
        sql = "SELECT * FROM hosts"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name COLLATE NOCASE ASC"
        async with db.execute(sql) as cur:
            rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["enabled"] = bool(d.get("enabled"))
            out.append(d)
        return out

    async def get_host(self, host_id: int) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["enabled"] = bool(d.get("enabled"))
        return d

    async def upsert_host(
        self,
        *,
        host_id: int | None,
        name: str,
        host: str,
        port: int = 22,
        ssh_user: str | None = None,
        enabled: bool = True,
        note: str = "",
    ) -> int:
        db = self._require()
        stamp, stamp_iso = self._stamp()
        user = (ssh_user or "").strip() or None
        if host_id is not None:
            await db.execute(
                """
                UPDATE hosts SET name=?, host=?, port=?, ssh_user=?, enabled=?,
                    note=?, updated_at=?, updated_at_iso=?
                WHERE id=?
                """,
                (
                    name,
                    host,
                    int(port),
                    user,
                    1 if enabled else 0,
                    note or "",
                    stamp,
                    stamp_iso,
                    host_id,
                ),
            )
            await db.commit()
            return int(host_id)
        cur = await db.execute(
            """
            INSERT INTO hosts (
                name, host, port, ssh_user, enabled, note,
                created_at, created_at_iso, updated_at, updated_at_iso
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                host,
                int(port),
                user,
                1 if enabled else 0,
                note or "",
                stamp,
                stamp_iso,
                stamp,
                stamp_iso,
            ),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def delete_host(self, host_id: int) -> None:
        db = self._require()
        await db.execute("DELETE FROM hosts WHERE id = ?", (host_id,))
        await db.commit()

    # --- per-target monitor prefs (topology guests + manual) ---

    async def list_unmonitored_ids(self) -> set[str]:
        db = self._require()
        async with db.execute(
            "SELECT target_id FROM target_prefs WHERE monitored = 0"
        ) as cur:
            rows = await cur.fetchall()
        return {str(r[0]) for r in rows if r[0]}

    async def set_monitored(self, target_id: str, monitored: bool) -> None:
        db = self._require()
        tid = (target_id or "").strip()
        if not tid:
            return
        stamp, stamp_iso = self._stamp()
        if monitored:
            await db.execute(
                "DELETE FROM target_prefs WHERE target_id = ?", (tid,)
            )
        else:
            await db.execute(
                """
                INSERT INTO target_prefs (target_id, monitored, updated_at, updated_at_iso)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    monitored=0, updated_at=excluded.updated_at,
                    updated_at_iso=excluded.updated_at_iso
                """,
                (tid, stamp, stamp_iso),
            )
        await db.commit()

    # --- scans ---

    async def create_scan(
        self,
        *,
        target_id: str,
        target_name: str,
        status: str = "running",
    ) -> int:
        db = self._require()
        stamp, stamp_iso = self._stamp()
        cur = await db.execute(
            """
            INSERT INTO scans (
                target_id, target_name, status, created_at, created_at_iso
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (target_id, target_name, status, stamp, stamp_iso),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def finish_scan(
        self,
        scan_id: int,
        *,
        status: str,
        pm: str | None = None,
        distro: str | None = None,
        summary: dict[str, Any] | None = None,
        llm_summary: str | None = None,
        reboot_required: bool = False,
        error_message: str | None = None,
        packages: list[dict[str, Any]] | None = None,
    ) -> None:
        db = self._require()
        stamp, stamp_iso = self._stamp()
        await db.execute(
            """
            UPDATE scans SET status=?, pm=?, distro=?, summary_json=?, llm_summary=?,
                reboot_required=?, error_message=?, finished_at=?, finished_at_iso=?
            WHERE id=?
            """,
            (
                status,
                pm,
                distro,
                json.dumps(summary or {}, ensure_ascii=False),
                llm_summary,
                1 if reboot_required else 0,
                error_message,
                stamp,
                stamp_iso,
                scan_id,
            ),
        )
        if packages is not None:
            await db.execute("DELETE FROM packages WHERE scan_id = ?", (scan_id,))
            for pkg in packages:
                await db.execute(
                    """
                    INSERT INTO packages (
                        scan_id, name, current_version, candidate_version,
                        priority, meta_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan_id,
                        pkg.get("name") or "",
                        pkg.get("current") or pkg.get("current_version"),
                        pkg.get("candidate") or pkg.get("candidate_version"),
                        pkg.get("priority") or "normal",
                        json.dumps(pkg.get("meta") or {}, ensure_ascii=False),
                    ),
                )
        await db.commit()

    async def set_scan_llm_summary(self, scan_id: int, text: str) -> None:
        db = self._require()
        await db.execute(
            "UPDATE scans SET llm_summary = ? WHERE id = ?",
            (text, scan_id),
        )
        await db.commit()

    async def get_scan(self, scan_id: int) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return self._scan_dict(row)

    async def list_packages(self, scan_id: int) -> list[dict[str, Any]]:
        db = self._require()
        async with db.execute(
            "SELECT * FROM packages WHERE scan_id = ? ORDER BY priority ASC, name ASC",
            (scan_id,),
        ) as cur:
            rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            raw = d.pop("meta_json", None)
            if isinstance(raw, str) and raw:
                try:
                    d["meta"] = json.loads(raw)
                except json.JSONDecodeError:
                    d["meta"] = {}
            else:
                d["meta"] = {}
            out.append(d)
        return out

    async def latest_scan_for_target(self, target_id: str) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute(
            """
            SELECT * FROM scans WHERE target_id = ?
            ORDER BY created_at_iso DESC LIMIT 1
            """,
            (target_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return self._scan_dict(row)

    async def list_scans(self, *, limit: int = 50) -> list[dict[str, Any]]:
        db = self._require()
        async with db.execute(
            "SELECT * FROM scans ORDER BY created_at_iso DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._scan_dict(r) for r in rows]

    def _scan_dict(self, row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        d["reboot_required"] = bool(d.get("reboot_required"))
        raw = d.get("summary_json")
        if isinstance(raw, str) and raw:
            try:
                d["summary"] = json.loads(raw)
            except json.JSONDecodeError:
                d["summary"] = {}
        else:
            d["summary"] = {}
        return d

    # --- apply runs ---

    async def create_apply_run(
        self,
        *,
        target_id: str,
        target_name: str,
        package_filter: str,
        packages: list[str] | None = None,
        pm: str | None = None,
    ) -> int:
        db = self._require()
        stamp, stamp_iso = self._stamp()
        cur = await db.execute(
            """
            INSERT INTO apply_runs (
                target_id, target_name, package_filter, packages_json,
                status, pm, created_at, created_at_iso
            ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
            """,
            (
                target_id,
                target_name,
                package_filter,
                json.dumps(packages or [], ensure_ascii=False),
                pm,
                stamp,
                stamp_iso,
            ),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def finish_apply_run(
        self,
        run_id: int,
        *,
        status: str,
        log_text: str | None = None,
        reboot_required: bool = False,
        error_message: str | None = None,
        pm: str | None = None,
    ) -> None:
        db = self._require()
        stamp, stamp_iso = self._stamp()
        await db.execute(
            """
            UPDATE apply_runs SET status=?, log_text=?, reboot_required=?,
                error_message=?, pm=COALESCE(?, pm),
                finished_at=?, finished_at_iso=?
            WHERE id=?
            """,
            (
                status,
                log_text,
                1 if reboot_required else 0,
                error_message,
                pm,
                stamp,
                stamp_iso,
                run_id,
            ),
        )
        await db.commit()

    async def get_apply_run(self, run_id: int) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute(
            "SELECT * FROM apply_runs WHERE id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return self._apply_dict(row)

    async def list_apply_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        db = self._require()
        async with db.execute(
            "SELECT * FROM apply_runs ORDER BY created_at_iso DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._apply_dict(r) for r in rows]

    def _apply_dict(self, row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        d["reboot_required"] = bool(d.get("reboot_required"))
        raw = d.get("packages_json")
        if isinstance(raw, str) and raw:
            try:
                d["packages"] = json.loads(raw)
            except json.JSONDecodeError:
                d["packages"] = []
        else:
            d["packages"] = []
        return d

    # --- schedules ---

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

    async def upsert_schedule(
        self,
        *,
        schedule_id: int | None,
        target_id: str,
        cron_expr: str,
        preset: str = "daily",
        enabled: bool = True,
        note: str = "",
    ) -> int:
        db = self._require()
        stamp, stamp_iso = self._stamp()
        if schedule_id is not None:
            await db.execute(
                """
                UPDATE schedules SET target_id=?, cron_expr=?, preset=?,
                    enabled=?, note=?, updated_at=?, updated_at_iso=?
                WHERE id=?
                """,
                (
                    target_id,
                    cron_expr,
                    preset,
                    1 if enabled else 0,
                    note or "",
                    stamp,
                    stamp_iso,
                    schedule_id,
                ),
            )
            await db.commit()
            return int(schedule_id)
        cur = await db.execute(
            """
            INSERT INTO schedules (
                target_id, cron_expr, preset, enabled, note,
                created_at, created_at_iso, updated_at, updated_at_iso
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_id,
                cron_expr,
                preset,
                1 if enabled else 0,
                note or "",
                stamp,
                stamp_iso,
                stamp,
                stamp_iso,
            ),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def delete_schedule(self, schedule_id: int) -> None:
        db = self._require()
        await db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        await db.commit()

    # --- image scans ---

    async def create_image_scan(
        self,
        *,
        target_id: str,
        target_name: str,
        status: str = "running",
    ) -> int:
        db = self._require()
        stamp, stamp_iso = self._stamp()
        cur = await db.execute(
            """
            INSERT INTO image_scans (
                target_id, target_name, status, created_at, created_at_iso
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (target_id, target_name, status, stamp, stamp_iso),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def finish_image_scan(
        self,
        scan_id: int,
        *,
        status: str,
        update_count: int = 0,
        summary: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        db = self._require()
        stamp, stamp_iso = self._stamp()
        await db.execute(
            """
            UPDATE image_scans SET status=?, update_count=?, summary_json=?,
                error_message=?, finished_at=?, finished_at_iso=?
            WHERE id=?
            """,
            (
                status,
                int(update_count),
                json.dumps(summary or {}, ensure_ascii=False),
                error_message,
                stamp,
                stamp_iso,
                scan_id,
            ),
        )
        await db.commit()

    async def latest_image_scan_for_target(
        self, target_id: str, *, success_only: bool = False
    ) -> dict[str, Any] | None:
        db = self._require()
        sql = "SELECT * FROM image_scans WHERE target_id = ?"
        if success_only:
            sql += " AND status = 'success'"
        sql += " ORDER BY created_at_iso DESC LIMIT 1"
        async with db.execute(sql, (target_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return self._image_scan_dict(row)

    def _image_scan_dict(self, row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        raw = d.get("summary_json")
        if isinstance(raw, str) and raw:
            try:
                d["summary"] = json.loads(raw)
            except json.JSONDecodeError:
                d["summary"] = {}
        else:
            d["summary"] = {}
        return d

    # --- wave agent ---

    async def get_agent_settings(self) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute("SELECT * FROM agent_settings WHERE id = 1") as cur:
            row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        for key in ("enabled", "auto_security"):
            if d.get(key) is not None:
                d[key] = bool(d[key])
        if d.get("max_parallel") is not None:
            d["max_parallel"] = int(d["max_parallel"])
        return d

    async def set_agent_settings(
        self,
        *,
        enabled: bool,
        auto_security: bool,
        max_parallel: int,
    ) -> None:
        db = self._require()
        stamp, stamp_iso = self._stamp()
        await db.execute(
            """
            INSERT INTO agent_settings (
                id, enabled, auto_security, max_parallel, updated_at, updated_at_iso
            ) VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                enabled=excluded.enabled,
                auto_security=excluded.auto_security,
                max_parallel=excluded.max_parallel,
                updated_at=excluded.updated_at,
                updated_at_iso=excluded.updated_at_iso
            """,
            (
                1 if enabled else 0,
                1 if auto_security else 0,
                max(1, int(max_parallel)),
                stamp,
                stamp_iso,
            ),
        )
        await db.commit()

    def _wave_dict(self, row: aiosqlite.Row) -> dict[str, Any]:
        return dict(row)

    def _wave_item_dict(self, row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        d["needs_confirm"] = bool(d.get("needs_confirm"))
        d["confirmed"] = bool(d.get("confirmed"))
        for src, dest in (
            ("packages_json", "packages"),
            ("confirm_reasons_json", "confirm_reasons"),
            ("gate_json", "gates"),
        ):
            raw = d.pop(src, None)
            if isinstance(raw, str) and raw:
                try:
                    d[dest] = json.loads(raw)
                except json.JSONDecodeError:
                    d[dest] = []
            else:
                d[dest] = []
        return d

    async def create_wave(self, *, items: list[dict[str, Any]]) -> int:
        db = self._require()
        stamp, stamp_iso = self._stamp()
        cur = await db.execute(
            """
            INSERT INTO waves (
                status, created_at, created_at_iso, updated_at, updated_at_iso
            ) VALUES ('planned', ?, ?, ?, ?)
            """,
            (stamp, stamp_iso, stamp, stamp_iso),
        )
        wave_id = int(cur.lastrowid)
        for item in items:
            await db.execute(
                """
                INSERT INTO wave_items (
                    wave_id, target_id, target_name, bucket, sort_order,
                    needs_confirm, confirmed, status, package_filter,
                    packages_json, reason, confirm_reasons_json, gate_json,
                    explanation, created_at, created_at_iso
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    wave_id,
                    item.get("target_id") or "",
                    item.get("target_name") or "",
                    item.get("bucket") or "regular",
                    int(item.get("sort_order") or 0),
                    1 if item.get("needs_confirm") else 0,
                    item.get("status") or "planned",
                    item.get("package_filter") or "selected",
                    json.dumps(item.get("packages") or [], ensure_ascii=False),
                    item.get("reason") or "",
                    json.dumps(item.get("confirm_reasons") or [], ensure_ascii=False),
                    json.dumps(item.get("gates") or [], ensure_ascii=False),
                    item.get("explanation") or "",
                    stamp,
                    stamp_iso,
                ),
            )
        await db.commit()
        return wave_id

    async def get_wave(self, wave_id: int) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute("SELECT * FROM waves WHERE id = ?", (wave_id,)) as cur:
            row = await cur.fetchone()
        return self._wave_dict(row) if row else None

    async def get_latest_wave(self) -> dict[str, Any] | None:
        db = self._require()
        async with db.execute(
            "SELECT * FROM waves ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return self._wave_dict(row) if row else None

    async def delete_wave(self, wave_id: int) -> None:
        db = self._require()
        await db.execute("DELETE FROM wave_items WHERE wave_id = ?", (wave_id,))
        await db.execute("DELETE FROM waves WHERE id = ?", (wave_id,))
        await db.commit()

    async def update_wave(
        self,
        wave_id: int,
        *,
        status: str | None = None,
        stop_reason: str | None = None,
        failed_item_id: int | None = None,
    ) -> None:
        db = self._require()
        stamp, stamp_iso = self._stamp()
        fields = ["updated_at=?", "updated_at_iso=?"]
        vals: list[Any] = [stamp, stamp_iso]
        if status is not None:
            fields.append("status=?")
            vals.append(status)
            if status == "running":
                fields.append("started_at=COALESCE(started_at, ?)")
                fields.append("started_at_iso=COALESCE(started_at_iso, ?)")
                vals.extend([stamp, stamp_iso])
            if status in ("stopped", "failed", "completed"):
                fields.append("finished_at=?")
                fields.append("finished_at_iso=?")
                vals.extend([stamp, stamp_iso])
        if stop_reason is not None:
            fields.append("stop_reason=?")
            vals.append(stop_reason)
        if failed_item_id is not None:
            fields.append("failed_item_id=?")
            vals.append(int(failed_item_id))
        vals.append(wave_id)
        await db.execute(
            f"UPDATE waves SET {', '.join(fields)} WHERE id=?",
            vals,
        )
        await db.commit()

    async def list_wave_items(self, wave_id: int) -> list[dict[str, Any]]:
        db = self._require()
        async with db.execute(
            """
            SELECT * FROM wave_items
            WHERE wave_id = ?
            ORDER BY sort_order ASC, id ASC
            """,
            (wave_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._wave_item_dict(r) for r in rows]

    async def update_wave_item(
        self,
        item_id: int,
        *,
        status: str | None = None,
        confirmed: bool | None = None,
        gates: list[str] | None = None,
        job_id: str | None = None,
        error_message: str | None = None,
        explanation: str | None = None,
    ) -> None:
        db = self._require()
        fields: list[str] = []
        vals: list[Any] = []
        if status is not None:
            fields.append("status=?")
            vals.append(status)
        if confirmed is not None:
            fields.append("confirmed=?")
            vals.append(1 if confirmed else 0)
        if gates is not None:
            fields.append("gate_json=?")
            vals.append(json.dumps(gates, ensure_ascii=False))
        if job_id is not None:
            fields.append("job_id=?")
            vals.append(job_id)
        if error_message is not None:
            fields.append("error_message=?")
            vals.append(error_message)
        if explanation is not None:
            fields.append("explanation=?")
            vals.append(explanation)
        if not fields:
            return
        vals.append(item_id)
        await db.execute(
            f"UPDATE wave_items SET {', '.join(fields)} WHERE id=?",
            vals,
        )
        await db.commit()

    async def get_wave_item_by_job(self, job_id: str) -> dict[str, Any] | None:
        db = self._require()
        jid = (job_id or "").strip()
        if not jid:
            return None
        async with db.execute(
            "SELECT * FROM wave_items WHERE job_id = ? ORDER BY id DESC LIMIT 1",
            (jid,),
        ) as cur:
            row = await cur.fetchone()
        return self._wave_item_dict(row) if row else None
