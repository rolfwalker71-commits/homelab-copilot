"""Existing restic snapshots + tar archives for one compose stack card."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.locale import format_bytes, format_de, iso_utc, now_berlin

from backup_verifier.browser import ARCHIVE_SUFFIXES
from backup_verifier.config import BackupSettings, get_backup_settings
from backup_verifier.destinations import (
    KIND_COPILOT,
    KIND_SFTP,
    ensure_seeded,
    is_hetzner_storagebox,
)
from backup_verifier.restic import (
    ENGINE_RESTIC,
    ResticError,
    copilot_repo_path,
    list_local_restic_snapshots,
    safe_name,
)
from backup_verifier.scheduler import next_run_after
from backup_verifier.store import BackupStore

logger = logging.getLogger(__name__)

ENGINE_TAR = "tar"
CACHE_TTL_S = 90.0
CHECK_URL = "/modules/backup_verifier/check"

JOB_STATUS_DE = {
    "success": "OK",
    "failed": "fehlgeschlagen",
    "running": "läuft",
    "partial": "teilweise",
}

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class StackBackupError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def assert_stack_keys(parent_id: str, project: str) -> tuple[str, str]:
    """Reject empty keys and path traversal. Colons in parent_id are allowed."""
    parent_id = str(parent_id or "").strip()
    project = str(project or "").strip()
    if not parent_id or not project:
        raise StackBackupError("parent_id und project sind Pflicht.")
    for label, value in (("parent_id", parent_id), ("project", project)):
        if len(value) > 200:
            raise StackBackupError(f"{label} zu lang.")
        if ".." in value or "\x00" in value or "/" in value or "\\" in value:
            raise StackBackupError(f"Ungültiger {label}.")
    return parent_id, project


def cache_key(parent_id: str, project: str) -> str:
    return f"{parent_id}::{project}"


def cache_clear() -> None:
    _cache.clear()


def cache_get(parent_id: str, project: str) -> dict[str, Any] | None:
    key = cache_key(parent_id, project)
    hit = _cache.get(key)
    if not hit:
        return None
    ts, payload = hit
    if time.monotonic() - ts > CACHE_TTL_S:
        _cache.pop(key, None)
        return None
    return payload


def cache_put(parent_id: str, project: str, payload: dict[str, Any]) -> None:
    _cache[cache_key(parent_id, project)] = (time.monotonic(), payload)


def snapshot_kind(tags: list[Any] | None) -> str:
    for tag in tags or []:
        low = str(tag).strip().lower()
        if low == "full":
            return "full"
        if low in ("incr", "incremental"):
            return "incr"
    return ""


def parse_restic_time(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_when(dt: datetime | None) -> tuple[str | None, str | None]:
    if dt is None:
        return None, None
    return format_de(dt), iso_utc(dt)


def dir_size_bytes(path: Path, *, limit_files: int = 40_000) -> int | None:
    """Cheap on-disk size (no ``restic stats``)."""
    if not path.is_dir():
        return None
    total = 0
    n = 0
    skip = {".hc-ls", ".hc-drill", "_snap", "locks"}
    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in skip]
            for name in files:
                n += 1
                if n > limit_files:
                    return total
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        return None
    return total


def list_tar_archives(copilot_dir: Path, project: str, *, limit: int = 30) -> list[dict[str, Any]]:
    folder = Path(copilot_dir) / project
    if not folder.is_dir():
        alt = Path(copilot_dir) / safe_name(project)
        folder = alt if alt.is_dir() else folder
    if not folder.is_dir():
        return []
    files: list[Path] = []
    try:
        for child in folder.iterdir():
            if child.is_file() and child.name.lower().endswith(ARCHIVE_SUFFIXES):
                files.append(child)
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for path in files[: max(1, min(limit, 80))]:
        try:
            st = path.stat()
        except OSError:
            continue
        when, when_iso = format_when(
            datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        )
        size = int(st.st_size)
        out.append(
            {
                "engine": ENGINE_TAR,
                "name": path.name,
                "time": when,
                "time_iso": when_iso,
                "size_bytes": size,
                "size": format_bytes(size),
                "where": "copilot",
            }
        )
    return out


def shape_restic_items(
    snaps: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    by_snap: dict[str, dict[str, Any]] = {}
    for run in runs:
        sid = str(run.get("snapshot_id") or "").strip()
        if sid:
            by_snap[sid] = run
            by_snap[sid[:12]] = run
            by_snap[sid[:8]] = run
    items: list[dict[str, Any]] = []
    for snap in snaps[: max(1, min(limit, 80))]:
        tags = list(snap.get("tags") or [])
        kind = snapshot_kind(tags)
        sid = str(snap.get("id") or "")
        short = str(snap.get("short_id") or sid[:8])
        run = by_snap.get(sid) or by_snap.get(sid[:12]) or by_snap.get(short)
        size_bytes = None
        if run:
            raw = run.get("bytes_added")
            if raw is None:
                raw = run.get("size_bytes")
            try:
                size_bytes = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                size_bytes = None
        dt = parse_restic_time(str(snap.get("time") or ""))
        when, when_iso = format_when(dt)
        items.append(
            {
                "engine": ENGINE_RESTIC,
                "id": sid,
                "short_id": short,
                "time": when,
                "time_iso": when_iso,
                "kind": kind,
                "kind_label": (
                    "Voll" if kind == "full" else ("Inkrementell" if kind == "incr" else "Snapshot")
                ),
                "tags": [str(t) for t in tags if str(t) not in ("homelab-copilot",)],
                "size_bytes": size_bytes,
                "size": format_bytes(size_bytes) if size_bytes is not None else None,
                "where": "copilot",
            }
        )
    return items


def last_job_payload(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not run:
        return None
    status = str(run.get("status") or "").strip() or "unknown"
    return {
        "id": run.get("id"),
        "status": status,
        "status_label": JOB_STATUS_DE.get(status, status),
        "engine": str(run.get("engine") or ENGINE_TAR),
        "created_at": run.get("created_at"),
        "created_at_iso": run.get("created_at_iso"),
        "finished_at": run.get("finished_at"),
        "error_message": run.get("error_message") or None,
        "snapshot_id": run.get("snapshot_id") or None,
        "size": format_bytes(run.get("size_bytes")) if run.get("size_bytes") is not None else None,
    }


def schedule_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    nxt = next_run_after(str(row.get("cron_expr") or ""))
    engine = str(row.get("engine") or ENGINE_TAR)
    return {
        "id": row.get("id"),
        "enabled": bool(row.get("enabled")),
        "engine": engine,
        "engine_label": "Incremental (restic)" if engine == ENGINE_RESTIC else "Vollarchiv (tar)",
        "cron_expr": row.get("cron_expr"),
        "next_run": format_de(nxt) if nxt else None,
        "next_run_iso": iso_utc(nxt) if nxt else None,
        "keep_last": int(row.get("restic_keep_last") or 0) or None,
        "keep_weekly": int(row.get("restic_keep_weekly") or 0)
        if row.get("restic_keep_weekly") is not None
        else None,
        "full_every_days": int(row.get("restic_full_every_days") or 0) or None,
        "check_url": CHECK_URL,
    }


def _hop_status(hops: list[Any], *, kind: str, hetzner: bool = False) -> str | None:
    last: str | None = None
    for hop in hops:
        if not isinstance(hop, dict):
            continue
        if str(hop.get("kind") or "") != kind:
            continue
        if hetzner and not (
            is_hetzner_storagebox(dest=hop) or str(hop.get("preset") or "") == "storage_box"
        ):
            # still accept any SFTP hop when looking for dest
            if kind != KIND_SFTP:
                continue
        status = str(hop.get("status") or "").strip()
        if status:
            last = status
    return last


def where_payload(
    destinations: list[dict[str, Any]],
    last_run: dict[str, Any] | None,
    *,
    copilot_restic: bool,
    copilot_tar: bool,
) -> dict[str, Any]:
    hops = []
    if last_run and isinstance(last_run.get("destinations"), list):
        hops = last_run["destinations"]
    copilot_cfg = any(
        d.get("kind") == KIND_COPILOT and d.get("enabled") for d in destinations
    )
    sftp_rows = [d for d in destinations if d.get("kind") == KIND_SFTP and d.get("enabled")]
    hetzner = next((d for d in sftp_rows if is_hetzner_storagebox(dest=d)), None)
    dest = hetzner or (sftp_rows[0] if sftp_rows else None)
    dest_label = "Hetzner" if hetzner else str((dest or {}).get("label") or "SFTP")
    copilot_hop = _hop_status(hops, kind=KIND_COPILOT)
    if copilot_hop is None and last_run:
        legacy = str(last_run.get("copilot_status") or "").strip()
        if legacy and legacy not in ("pending", "—"):
            copilot_hop = legacy
    dest_hop = _hop_status(hops, kind=KIND_SFTP)
    if dest_hop is None and last_run:
        legacy = str(last_run.get("synology_status") or "").strip()
        if legacy and legacy not in ("pending", "—"):
            dest_hop = legacy
    dest_present: bool | None
    if dest_hop in ("ok", "success"):
        dest_present = True
    elif dest_hop in ("failed",):
        dest_present = False
    else:
        dest_present = None
    return {
        "copilot": {
            "configured": copilot_cfg,
            "present": bool(copilot_restic or copilot_tar),
            "restic": copilot_restic,
            "tar": copilot_tar,
            "last_hop": copilot_hop,
            "label": "Copilot",
        },
        "dest": {
            "configured": bool(dest),
            "present": dest_present,
            "hetzner": bool(hetzner),
            "label": dest_label if dest else "Hetzner",
            "last_hop": dest_hop,
        },
    }


def _pick_schedule(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    enabled = [r for r in rows if r.get("enabled")]
    if enabled:
        return enabled[0]
    return rows[0] if rows else None


def existing_backup_count(payload: dict[str, Any] | None) -> int:
    """Restic snapshots + tar archives on Copilot (not Verlauf rows)."""
    if not payload:
        return 0
    restic = payload.get("restic")
    tar = payload.get("tar")
    if isinstance(restic, list) or isinstance(tar, list):
        return len(restic or []) + len(tar or [])
    items = payload.get("items")
    if isinstance(items, list):
        return len(items)
    try:
        return max(0, int(payload.get("count") or 0))
    except (TypeError, ValueError):
        return 0


def sum_parent_existing_counts(payloads: list[dict[str, Any]] | None) -> int:
    """Sum existing backups across compose stacks of one parent_id."""
    return sum(existing_backup_count(p) for p in (payloads or []))


async def collect_stack_backups(
    store: BackupStore,
    *,
    parent_id: str,
    project: str,
    bsettings: BackupSettings | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    parent_id, project = assert_stack_keys(parent_id, project)
    if not refresh:
        cached = cache_get(parent_id, project)
        if cached is not None:
            return {**cached, "cached": True}

    bsettings = bsettings or get_backup_settings()
    await ensure_seeded(store, bsettings)
    destinations = await store.list_destinations()
    runs = await store.list_runs_for_stack(parent_id, project, limit=30)
    last_run = runs[0] if runs else None
    schedules = await store.find_schedules_for_stack(parent_id, project)
    schedule = schedule_payload(_pick_schedule(schedules))

    tar_items = list_tar_archives(bsettings.copilot_dir, project)
    repo = copilot_repo_path(bsettings.copilot_dir, parent_id, project)
    copilot_restic = (repo / "config").is_file()
    restic_error = None
    snaps: list[dict[str, Any]] = []
    repo_size = dir_size_bytes(repo) if copilot_restic else None
    password = await store.get_restic_password(parent_id, project)
    if copilot_restic and password:
        try:
            snaps = await list_local_restic_snapshots(
                repo,
                password,
                project=project,
                timeout=min(90.0, float(bsettings.backup_ssh_timeout)),
            )
        except ResticError as exc:
            restic_error = exc.message
            logger.info("stack backups restic list failed for %s/%s", parent_id, project)
        except Exception:
            restic_error = "restic-Snapshots auf Copilot nicht lesbar."
            logger.exception("stack backups restic list crashed")
    elif copilot_restic and not password:
        restic_error = "Kein restic-Passwort für diesen Stack gespeichert."

    restic_items = shape_restic_items(snaps, runs)
    items = restic_items + tar_items
    items.sort(key=lambda x: str(x.get("time_iso") or ""), reverse=True)

    empty = not items
    count = existing_backup_count({"restic": restic_items, "tar": tar_items, "items": items})
    payload: dict[str, Any] = {
        "ok": True,
        "cached": False,
        "parent_id": parent_id,
        "project": project,
        "empty": empty,
        "count": count,
        "empty_label": "Noch keine Backups" if empty else None,
        "items": items,
        "restic": restic_items,
        "tar": tar_items,
        "restic_error": restic_error,
        "repo_size_bytes": repo_size,
        "repo_size": format_bytes(repo_size) if repo_size is not None else None,
        "last_job": last_job_payload(last_run),
        "schedule": schedule,
        "check_url": CHECK_URL,
        "where": where_payload(
            destinations,
            last_run,
            copilot_restic=copilot_restic,
            copilot_tar=bool(tar_items),
        ),
        "time": format_de(now_berlin()),
    }
    cache_put(parent_id, project, payload)
    return payload
