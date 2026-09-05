"""Smart Backup Integrity Verifier — ModuleProtocol entrypoint."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

# Ensure ``modules/`` is importable as package root for sibling modules.
_MODULES_ROOT = Path(__file__).resolve().parent.parent
if str(_MODULES_ROOT) not in sys.path:
    sys.path.insert(0, str(_MODULES_ROOT))

from app.config import get_settings
from app.core.docker_control import DockerControlError
from app.core.locale import format_bytes, format_de, iso_utc, now_berlin
from app.core.topology import TopologyStore

from backup_verifier.backup import BackupError, list_backup_stacks, run_backup
from backup_verifier import browser as browse_mod
from backup_verifier.config import get_backup_settings
from backup_verifier import cron as cron_mod
from backup_verifier import destinations as dest_mod
from backup_verifier import sshutil
from backup_verifier.inventory import build_inventory, resolve_guest
from backup_verifier.jobs import JOBS
from backup_verifier.restic import ENGINE_RESTIC, ResticError, list_restic_snapshots
from backup_verifier.restore import RestoreError, run_restore
from backup_verifier.scheduler import (
    next_run_after,
    run_schedule_loop,
    schedule_clock_hm,
    schedule_start_sort_key,
)
from backup_verifier.store import BackupStore

logger = logging.getLogger(__name__)

router = APIRouter()
_store: BackupStore | None = None
_sched_task: asyncio.Task[None] | None = None

_PUSH_BODY_MAX = 200


def _truncate_push(text: str, max_len: int = _PUSH_BODY_MAX) -> str:
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _format_duration_s(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _dest_summary(run: dict[str, Any] | None) -> str | None:
    if not run:
        return None
    hops = run.get("destinations")
    parts: list[str] = []
    if isinstance(hops, list) and hops:
        for h in hops:
            if not isinstance(h, dict):
                continue
            label = str(h.get("label") or h.get("kind") or "Ziel").strip()
            status = str(h.get("status") or "—").strip()
            verify = str(h.get("verify") or "").strip()
            bit = f"{label}:{status}"
            if verify and verify not in ("—", status, "pending"):
                bit += f"/{verify}"
            parts.append(bit)
    else:
        for key, label in (
            ("lxc_status", "Host"),
            ("copilot_status", "Copilot"),
            ("synology_status", "SFTP"),
        ):
            val = run.get(key)
            if val and str(val) not in ("pending", "—"):
                parts.append(f"{label}:{val}")
    return ", ".join(parts) if parts else None


async def _notify_backup_finished(
    *,
    status: str,
    project: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    duration_s: float | None = None,
) -> None:
    """One Web Push per completed backup job (success / partial / failed)."""
    try:
        from app.core.push import send_push_to_all

        store = None
        try:
            from app.main import app as fastapi_app

            store = getattr(fastapi_app.state, "app_store", None)
        except Exception:
            store = None
        if store is None:
            return

        from app.core.push import push_allowed

        if status == "success" and not await push_allowed(store, "backup_success"):
            return
        if status == "partial" and not await push_allowed(store, "backup_partial"):
            return
        if status not in ("success", "partial") and not await push_allowed(
            store, "backup_failure"
        ):
            return

        run = result or {}
        stack = str(run.get("stack") or project or "Stack").strip()
        guest = str(run.get("guest_name") or "").strip()
        subject = f"{stack} @ {guest}" if guest else stack

        if status == "success":
            title = "HomelabOps — Backup OK"
        elif status == "partial":
            title = "HomelabOps — Backup teilweise"
        else:
            title = "HomelabOps — Backup fehlgeschlagen"

        bits: list[str] = [subject]
        dest = _dest_summary(run)
        if dest:
            bits.append(dest)
        size_raw = run.get("size_bytes")
        if size_raw is not None:
            bits.append(format_bytes(size_raw))
        dur = _format_duration_s(duration_s)
        if dur:
            bits.append(dur)
        verify = str(run.get("verify_status") or "").strip()
        if verify and verify not in ("pending", "—"):
            bits.append(f"Verify:{verify}")
        if status == "failed" or (status == "partial" and error):
            err = (error or run.get("error_message") or "").strip()
            if err:
                # Keep error short; never include credential-looking fragments.
                err = err.replace("\n", " ")
                if len(err) > 80:
                    err = err[:79].rstrip() + "…"
                bits.append(f"Fehler: {err}")

        body = _truncate_push(" · ".join(bits))
        run_id = run.get("id")
        url = (
            f"/modules/backup_verifier/history?run={run_id}"
            if run_id is not None
            else "/modules/backup_verifier/history"
        )
        await send_push_to_all(
            store,
            title=title,
            body=body,
            url=url,
            tag="backup-finished",
        )
    except Exception:
        logger.exception("Push-Benachrichtigung (Backup) fehlgeschlagen")


async def _maybe_load_run(run_id: int | None) -> dict[str, Any] | None:
    if run_id is None or _store is None:
        return None
    try:
        return await _store.get_run(int(run_id))
    except Exception:
        logger.exception("Backup-Lauf für Push nicht ladbar")
        return None


def _make_templates() -> Jinja2Templates:
    """Module templates + app base.html (ChoiceLoader)."""
    module_dir = Path(__file__).resolve().parent / "templates"
    app_dir = Path(__file__).resolve().parents[2] / "app" / "templates"
    env = Environment(
        loader=ChoiceLoader(
            [
                FileSystemLoader(str(module_dir)),
                FileSystemLoader(str(app_dir)),
            ]
        ),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.globals["format_de"] = format_de
    env.globals["format_bytes"] = format_bytes
    templates = Jinja2Templates(directory=str(module_dir))
    templates.env = env
    return templates


def _get_store() -> BackupStore:
    if _store is None:
        raise HTTPException(status_code=503, detail="Backup-Store nicht bereit.")
    return _store


def _snapshot(request: Request):
    store: TopologyStore = request.app.state.topology_store
    return store.snapshot


def _with_guest(row: dict[str, Any], snapshot: Any, *, overwrite: bool = False) -> dict[str, Any]:
    """Attach guest_name / guest_ip from topology (parent_id like lxc:pve01:105)."""
    out = dict(row)
    existing = str(out.get("guest_name") or "").strip()
    if existing and not overwrite:
        if not out.get("guest_ip"):
            info = resolve_guest(snapshot, str(out.get("parent_id") or ""))
            if info.get("guest_ip"):
                out["guest_ip"] = info["guest_ip"]
        return out
    info = resolve_guest(snapshot, str(out.get("parent_id") or ""))
    out["guest_name"] = info["guest_name"] or existing
    if info.get("guest_ip"):
        out["guest_ip"] = info["guest_ip"]
    return out


async def _run_enqueued_backup(
    job_id: str,
    *,
    store: BackupStore,
    parent_id: str,
    project: str,
    snapshot: Any,
    quiesce: bool | None,
    engine: str = "tar",
    restic_full_every_days: int = 7,
    restic_keep_last: int = 14,
    restic_keep_weekly: int = 8,
) -> None:
    JOBS.set_running(job_id)
    job = JOBS.get(job_id)
    t0 = job.created_at if job else time.time()

    async def on_progress(
        *,
        phase: str,
        percent: int,
        message: str,
        run_id: int | None = None,
    ) -> None:
        JOBS.set_progress(
            job_id,
            phase=phase,
            percent=percent,
            message=message,
            run_id=run_id,
        )

    async def on_log(line: str) -> None:
        JOBS.append_log(job_id, line)

    try:
        result = await run_backup(
            store,
            parent_id=parent_id,
            project=project,
            snapshot=snapshot,
            quiesce=quiesce,
            engine=engine,
            restic_full_every_days=restic_full_every_days,
            restic_keep_last=restic_keep_last,
            restic_keep_weekly=restic_keep_weekly,
            on_progress=on_progress,
            on_log=on_log,
        )
        status = str((result or {}).get("status") or "success")
        norm = status if status in ("success", "partial", "failed") else "success"
        JOBS.finish(job_id, status=norm, result=result)
        await _notify_backup_finished(
            status=norm,
            project=project,
            result=result,
            error=(result or {}).get("error_message"),
            duration_s=time.time() - t0,
        )
    except BackupError as exc:
        current = JOBS.get(job_id)
        run = await _maybe_load_run(current.run_id if current else None)
        JOBS.finish(
            job_id,
            status="failed",
            error=exc.message,
            phase="Fehler",
            message=exc.message,
            result=run,
        )
        await _notify_backup_finished(
            status="failed",
            project=project,
            result=run,
            error=exc.message,
            duration_s=time.time() - t0,
        )
    except DockerControlError as exc:
        current = JOBS.get(job_id)
        run = await _maybe_load_run(current.run_id if current else None)
        JOBS.finish(
            job_id,
            status="failed",
            error=exc.message,
            phase="Fehler",
            message=exc.message,
            result=run,
        )
        await _notify_backup_finished(
            status="failed",
            project=project,
            result=run,
            error=exc.message,
            duration_s=time.time() - t0,
        )
    except Exception as exc:
        logger.exception("Background backup failed")
        msg = str(exc) or "Unbekannter Fehler beim Backup."
        current = JOBS.get(job_id)
        run = await _maybe_load_run(current.run_id if current else None)
        JOBS.finish(
            job_id,
            status="failed",
            error=msg,
            phase="Fehler",
            message=msg,
            result=run,
        )
        await _notify_backup_finished(
            status="failed",
            project=project,
            result=run,
            error=msg,
            duration_s=time.time() - t0,
        )


def _enqueue_backup(
    *,
    store: BackupStore,
    parent_id: str,
    project: str,
    snapshot: Any,
    quiesce: bool | None = None,
    engine: str = "tar",
    restic_full_every_days: int = 7,
    restic_keep_last: int = 14,
    restic_keep_weekly: int = 8,
):
    job = JOBS.create(parent_id=parent_id, project=project)

    async def _bg() -> None:
        await _run_enqueued_backup(
            job.id,
            store=store,
            parent_id=parent_id,
            project=project,
            snapshot=snapshot,
            quiesce=quiesce,
            engine=engine,
            restic_full_every_days=restic_full_every_days,
            restic_keep_last=restic_keep_last,
            restic_keep_weekly=restic_keep_weekly,
        )

    return job, _bg


async def _fire_scheduled_backup(app: FastAPI, schedule: dict[str, Any]) -> None:
    store = _get_store()
    topo: TopologyStore = app.state.topology_store
    snap = topo.snapshot
    if snap is None:
        raise RuntimeError("Keine Topologie — geplantes Backup übersprungen.")
    job, coro = _enqueue_backup(
        store=store,
        parent_id=str(schedule["parent_id"]),
        project=str(schedule["stack"]),
        snapshot=snap,
        engine=str(schedule.get("engine") or "tar"),
        restic_full_every_days=int(schedule.get("restic_full_every_days") or 7),
        restic_keep_last=int(schedule.get("restic_keep_last") or 14),
        restic_keep_weekly=int(schedule.get("restic_keep_weekly") or 8),
    )
    asyncio.create_task(coro(), name=f"backup-sched-{job.id}")


class RunPayload(BaseModel):
    parent_id: str = Field(..., min_length=1)
    project: str = Field(..., min_length=1)
    quiesce: bool | None = None
    engine: str = Field(default="tar", pattern="^(tar|restic)$")
    restic_full_every_days: int = Field(default=7, ge=1, le=365)
    restic_keep_last: int = Field(default=14, ge=1, le=365)
    restic_keep_weekly: int = Field(default=8, ge=0, le=104)


class RestorePayload(BaseModel):
    confirm: bool = False
    # Destination id (int as string), "copilot", "synology", or kind/label
    source: str = Field(default="copilot", min_length=1, max_length=64)
    snapshot_id: str | None = Field(default=None, max_length=64)


class DestinationsPayload(BaseModel):
    destinations: list[dict[str, Any]] = Field(default_factory=list)


class DestinationCheckPayload(BaseModel):
    destination: dict[str, Any] = Field(default_factory=dict)


class SchedulePayload(BaseModel):
    id: int | None = None
    parent_id: str = Field(..., min_length=1)
    stack: str = Field(..., min_length=1)
    preset: str = Field(default="daily", pattern="^(daily|weekly|custom)$")
    time: str = Field(default="03:00")
    weekday: int = Field(default=0, ge=0, le=6)
    cron_expr: str | None = None
    enabled: bool = True
    note: str = ""
    engine: str = Field(default="tar", pattern="^(tar|restic)$")
    restic_full_every_days: int = Field(default=7, ge=1, le=365)
    restic_keep_last: int = Field(default=14, ge=1, le=365)
    restic_keep_weekly: int = Field(default=8, ge=0, le=104)


@router.get("/status")
async def module_status() -> dict[str, Any]:
    bs = get_backup_settings()
    s = get_settings()
    store = _store
    pipeline: list[dict[str, Any]] = []
    if store is not None:
        await dest_mod.ensure_seeded(store, bs)
        rows = await store.list_destinations()
        pipeline = [dest_mod.public_destination(r) for r in rows]
    return {
        "module": "backup_verifier",
        "version": "0.3.0",
        "time": format_de(now_berlin()),
        "copilot_dir": str(bs.copilot_dir),
        "lxc_dir": bs.backup_lxc_dir,
        "keep": {
            "lxc": bs.backup_lxc_keep,
            "copilot": bs.backup_copilot_keep,
            "synology": bs.backup_synology_keep,
        },
        "quiesce_default": bs.backup_quiesce,
        "restic_install": bs.restic_install,
        "restic_install_timeout": bs.restic_install_timeout,
        "backup_rsync_install": bs.backup_rsync_install,
        "backup_rsync_install_timeout": bs.backup_rsync_install_timeout,
        "pipeline": pipeline,
        "synology": {
            "configured": any(
                p.get("kind") == "sftp" and p.get("enabled") and p.get("preset") == "synology"
                for p in pipeline
            )
            or bs.synology_configured,
            "host": bs.backup_synology_host or None,
            "user": bs.backup_synology_user or None,
            "path": bs.backup_synology_path or None,
            "key_present": bs.synology_key().is_file(),
        },
        "ssh_key_present": Path(s.docker_ssh_key_path).is_file()
        or (Path(s.data_dir) / "ssh" / "id_ed25519").is_file(),
        "scheduler": "in_process",
        "scheduler_running": bool(_sched_task and not _sched_task.done()),
        "timezone": "Europe/Berlin",
        "crontab_available": cron_mod.crontab_available(),
        "api_base": bs.backup_api_base,
        "disclaimer": (
            "Stack-Backup für typische Compose-Stacks. Bind-Mounts nur wenn lesbar. "
            "Kein Ersatz für Proxmox vzdump (volle LXC-DR)."
        ),
    }


@router.get("/destinations")
async def list_destinations() -> dict[str, Any]:
    store = _get_store()
    await dest_mod.ensure_seeded(store)
    rows = await store.list_destinations()
    return {
        "destinations": [dest_mod.public_destination(r) for r in rows],
        "time": format_de(now_berlin()),
    }


@router.put("/destinations")
async def save_destinations(payload: DestinationsPayload) -> dict[str, Any]:
    store = _get_store()
    await dest_mod.ensure_seeded(store)
    existing = await store.list_destinations()
    try:
        normalized = dest_mod.normalize_incoming(
            payload.destinations, existing=existing
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = await store.replace_destinations(normalized)
    return {
        "ok": True,
        "destinations": [dest_mod.public_destination(r) for r in rows],
        "message": "Ziele gespeichert.",
        "time": format_de(now_berlin()),
    }


@router.post("/destinations/check")
async def check_destination(payload: DestinationCheckPayload) -> dict[str, Any]:
    store = _get_store()
    raw = dict(payload.destination or {})
    # If id given and secret blank, merge stored secret
    rid = raw.get("id")
    if rid is not None and not raw.get("secret_ref"):
        existing = await store.get_destination(int(rid))
        if existing:
            raw["secret_ref"] = existing.get("secret_ref") or ""
    result = await dest_mod.check_destination(raw)
    return result


async def _load_browse_dest(dest_id: int) -> dict[str, Any]:
    store = _get_store()
    await dest_mod.ensure_seeded(store)
    dest = await store.get_destination(int(dest_id))
    if not dest:
        raise HTTPException(status_code=404, detail="Ziel nicht gefunden.")
    return dest


@router.get("/browse")
async def browse_destination(
    dest_id: int = Query(..., ge=1),
    path: str = Query("", max_length=1024),
) -> dict[str, Any]:
    dest = await _load_browse_dest(dest_id)
    try:
        return await browse_mod.browse_destination(dest, path)
    except browse_mod.BrowserError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/browse/download")
async def browse_download(
    dest_id: int = Query(..., ge=1),
    path: str = Query(..., min_length=1, max_length=1024),
):
    dest = await _load_browse_dest(dest_id)
    if not browse_mod.is_browsable(dest):
        raise HTTPException(
            status_code=400,
            detail="Host-Staging ist ephemer und nicht durchsuchbar.",
        )
    try:
        rel = browse_mod.normalize_rel(path)
        root = browse_mod.dest_root(dest)
        name = rel.rsplit("/", 1)[-1] if rel else ""
        if dest.get("kind") == dest_mod.KIND_COPILOT:
            local = browse_mod.resolve_local_file(root, rel)
            headers = {
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{quote(local.name)}"
                ),
                "Cache-Control": "no-store",
            }
            return FileResponse(
                path=str(local),
                filename=local.name,
                media_type="application/gzip",
                headers=headers,
            )
        remote = browse_mod.assert_sftp_downloadable(root, rel, name)
        host = (dest.get("host") or "").strip()
        if not host:
            raise browse_mod.BrowserError("SFTP-Ziel hat keinen Host.", 400)
        settings = get_settings()
        auth = dest_mod.resolve_auth(dest, settings)
        try:
            await sshutil.sftp_stat_file(
                settings,
                host,
                remote,
                username=auth["username"],
                key=auth.get("key"),
                key_pem=auth.get("key_pem"),
                password=auth.get("password"),
                port=auth["port"],
            )
        except DockerControlError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
            "Cache-Control": "no-store",
        }
        return StreamingResponse(
            sshutil.sftp_iter_file(
                settings,
                host,
                remote,
                username=auth["username"],
                key=auth.get("key"),
                key_pem=auth.get("key_pem"),
                password=auth.get("password"),
                port=auth["port"],
            ),
            media_type="application/gzip",
            headers=headers,
        )
    except browse_mod.BrowserError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except DockerControlError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/stacks")
async def stacks(request: Request) -> dict[str, Any]:
    return {"stacks": await list_backup_stacks(_snapshot(request))}


@router.get("/preflight")
async def preflight(
    request: Request,
    parent_id: str = Query(..., min_length=1),
    project: str = Query(..., min_length=1),
) -> dict[str, Any]:
    bs = get_backup_settings()
    try:
        inv = await build_inventory(
            get_settings(),
            parent_id=parent_id,
            project=project,
            snapshot=_snapshot(request),
            lxc_backup_dir=bs.backup_lxc_dir,
        )
    except DockerControlError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {"ok": True, "inventory": inv}


@router.post("/run")
async def run_backup_endpoint(
    payload: RunPayload,
    request: Request,
    background: BackgroundTasks,
    wait: bool = Query(False),
) -> dict[str, Any]:
    """Start backup. Default: background job with progress; ``wait=true`` sync."""
    store = _get_store()
    snap = _snapshot(request)

    if wait:
        t0 = time.time()
        try:
            result = await run_backup(
                store,
                parent_id=payload.parent_id,
                project=payload.project,
                snapshot=snap,
                quiesce=payload.quiesce,
                engine=payload.engine,
                restic_full_every_days=payload.restic_full_every_days,
                restic_keep_last=payload.restic_keep_last,
                restic_keep_weekly=payload.restic_keep_weekly,
            )
            status = str((result or {}).get("status") or "success")
            await _notify_backup_finished(
                status=status if status in ("success", "partial", "failed") else "success",
                project=payload.project,
                result=result,
                error=(result or {}).get("error_message"),
                duration_s=time.time() - t0,
            )
            return {"ok": True, "run": result}
        except BackupError as exc:
            await _notify_backup_finished(
                status="failed",
                project=payload.project,
                error=exc.message,
                duration_s=time.time() - t0,
            )
            raise HTTPException(status_code=409, detail=exc.message) from exc
        except DockerControlError as exc:
            await _notify_backup_finished(
                status="failed",
                project=payload.project,
                error=exc.message,
                duration_s=time.time() - t0,
            )
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    job, _bg = _enqueue_backup(
        store=store,
        parent_id=payload.parent_id,
        project=payload.project,
        snapshot=snap,
        quiesce=payload.quiesce,
        engine=payload.engine,
        restic_full_every_days=payload.restic_full_every_days,
        restic_keep_last=payload.restic_keep_last,
        restic_keep_weekly=payload.restic_keep_weekly,
    )
    background.add_task(_bg)
    return {
        "ok": True,
        "started": True,
        "job_id": job.id,
        "message": (
            f"Backup für „{payload.project}“ gestartet. "
            "Fortschritt wird angezeigt."
        ),
        "history_url": "/modules/backup_verifier/history",
        "job_url": f"/api/modules/backup_verifier/jobs/{job.id}",
    }


@router.get("/jobs")
async def list_backup_jobs(active: bool = True) -> dict[str, Any]:
    jobs = JOBS.list_active() if active else []
    return {"ok": True, "jobs": [j.to_dict() for j in jobs]}


@router.get("/jobs/{job_id}")
async def get_backup_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden.")
    return {"ok": True, "job": job.to_dict()}


@router.get("/history")
async def history(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    stack: str | None = None,
) -> dict[str, Any]:
    store = _get_store()
    snapshot = _snapshot(request)
    runs = [_with_guest(r, snapshot) for r in await store.list_runs(limit=limit, stack=stack)]
    return {"runs": runs, "time": format_de(now_berlin())}


@router.get("/history/{run_id}")
async def history_detail(run_id: int) -> dict[str, Any]:
    store = _get_store()
    run = await store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Lauf nicht gefunden.")
    return {"run": run}


@router.post("/history/{run_id}/restore")
async def restore_endpoint(
    run_id: int,
    payload: RestorePayload,
    request: Request,
) -> dict[str, Any]:
    store = _get_store()
    try:
        result = await run_restore(
            store,
            run_id=run_id,
            snapshot=_snapshot(request),
            confirm=payload.confirm,
            source=payload.source,
            snapshot_id=payload.snapshot_id,
        )
        return result
    except RestoreError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except DockerControlError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


class ResticRestorePayload(BaseModel):
    parent_id: str = Field(..., min_length=1)
    project: str = Field(..., min_length=1)
    snapshot_id: str = Field(..., min_length=8, max_length=64)
    confirm: bool = False
    source: str = Field(default="copilot", min_length=1, max_length=64)


@router.get("/restic/snapshots")
async def restic_snapshots(
    request: Request,
    parent_id: str = Query(..., min_length=1),
    project: str = Query(..., min_length=1),
    source: str = Query("copilot", min_length=1, max_length=64),
) -> dict[str, Any]:
    store = _get_store()
    bs = get_backup_settings()
    meta = await store.get_restic_secret_meta(parent_id, project)
    if not meta:
        return {
            "ok": True,
            "engine": ENGINE_RESTIC,
            "snapshots": [],
            "hint": (
                "Noch kein restic-Backup für diesen Stack. "
                "Engine „Incremental (restic)“ wählen und einmal sichern."
            ),
        }
    try:
        inv = await build_inventory(
            get_settings(),
            parent_id=parent_id,
            project=project,
            snapshot=_snapshot(request),
            lxc_backup_dir=bs.backup_lxc_dir,
        )
        snaps = await list_restic_snapshots(
            store,
            parent_id=parent_id,
            project=project,
            inventory=inv,
            settings=get_settings(),
            bsettings=bs,
            source=source,
        )
    except ResticError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except DockerControlError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {
        "ok": True,
        "engine": ENGINE_RESTIC,
        "parent_id": parent_id,
        "project": project,
        "snapshots": snaps,
        "has_password": True,
        "last_full_at": meta.get("last_full_at"),
    }


@router.post("/restic/restore")
async def restic_restore_endpoint(
    payload: ResticRestorePayload,
    request: Request,
) -> dict[str, Any]:
    """Restore a restic snapshot (uses latest matching run for history linkage)."""
    store = _get_store()
    runs = await store.list_runs(limit=50, stack=payload.project)
    run = next(
        (
            r
            for r in runs
            if r.get("parent_id") == payload.parent_id
            and str(r.get("engine") or "") == ENGINE_RESTIC
            and r.get("status") in ("success", "partial")
        ),
        None,
    )
    if not run:
        raise HTTPException(
            status_code=400,
            detail=(
                "Kein erfolgreicher restic-Lauf für diesen Stack — "
                "zuerst ein Incremental-Backup ausführen."
            ),
        )
    try:
        result = await run_restore(
            store,
            run_id=int(run["id"]),
            snapshot=_snapshot(request),
            confirm=payload.confirm,
            source=payload.source,
            snapshot_id=payload.snapshot_id,
        )
        return result
    except RestoreError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except DockerControlError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/restores")
async def restores(limit: int = Query(30, ge=1, le=100)) -> dict[str, Any]:
    store = _get_store()
    return {"restores": await store.list_restores(limit=limit)}


@router.get("/schedules")
async def list_schedules(request: Request) -> dict[str, Any]:
    store = _get_store()
    schedules = await store.list_schedules()
    snapshot = _snapshot(request)
    now = now_berlin()
    enriched: list[dict[str, Any]] = []
    for row in schedules:
        nxt = next_run_after(str(row.get("cron_expr") or ""), now) if row.get("enabled") else None
        item = _with_guest(row, snapshot, overwrite=True)
        item["next_run"] = format_de(nxt) if nxt else None
        item["next_run_iso"] = iso_utc(nxt) if nxt else None
        hm = schedule_clock_hm(item, nxt)
        item["start_hm"] = f"{hm[0]:02d}:{hm[1]:02d}" if hm else None
        enriched.append(item)
    start_counts: dict[str, int] = {}
    for item in enriched:
        key = item.get("start_hm")
        if item.get("enabled") and key:
            start_counts[key] = start_counts.get(key, 0) + 1
    for item in enriched:
        key = item.get("start_hm")
        item["same_start"] = bool(
            item.get("enabled") and key and start_counts.get(key, 0) > 1
        )
    enriched.sort(key=schedule_start_sort_key)
    return {
        "schedules": enriched,
        "scheduler": "in_process",
        "scheduler_running": bool(_sched_task and not _sched_task.done()),
        "timezone": "Europe/Berlin",
        "crontab_available": cron_mod.crontab_available(),
        "preview": cron_mod.preview_crontab(enriched),
        "hint": (
            "Zeitpläne laufen in der App (Europe/Berlin). "
            "Kein Host-Crontab nötig — alte curl-Einträge kannst du löschen."
        ),
    }


async def _persist_schedule(
    payload: SchedulePayload, *, schedule_id: int | None
) -> dict[str, Any]:
    store = _get_store()
    if schedule_id is not None:
        existing = await store.get_schedule(schedule_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Zeitplan nicht gefunden.")
    try:
        if payload.preset == "custom":
            if not payload.cron_expr:
                raise cron_mod.CronError("custom erfordert cron_expr")
            expr = cron_mod.validate_cron_expr(payload.cron_expr)
        else:
            expr = cron_mod.preset_to_cron(
                payload.preset, payload.time, payload.weekday
            )
    except cron_mod.CronError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    try:
        sid = await store.upsert_schedule(
            schedule_id=schedule_id,
            stack=payload.stack,
            parent_id=payload.parent_id,
            cron_expr=expr,
            preset=payload.preset,
            enabled=payload.enabled,
            note=payload.note,
            engine=payload.engine,
            restic_full_every_days=payload.restic_full_every_days,
            restic_keep_last=payload.restic_keep_last,
            restic_keep_weekly=payload.restic_keep_weekly,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    schedules = await store.list_schedules()
    sync = cron_mod.sync_crontab(schedules)
    return {"ok": True, "id": sid, "cron_expr": expr, "crontab": sync}


@router.post("/schedules")
async def save_schedule(payload: SchedulePayload) -> dict[str, Any]:
    return await _persist_schedule(payload, schedule_id=payload.id)


@router.put("/schedules/{schedule_id}")
async def update_schedule(schedule_id: int, payload: SchedulePayload) -> dict[str, Any]:
    return await _persist_schedule(payload, schedule_id=schedule_id)


@router.patch("/schedules/{schedule_id}")
async def patch_schedule(schedule_id: int, payload: SchedulePayload) -> dict[str, Any]:
    return await _persist_schedule(payload, schedule_id=schedule_id)


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int) -> dict[str, Any]:
    store = _get_store()
    await store.delete_schedule(schedule_id)
    schedules = await store.list_schedules()
    sync = cron_mod.sync_crontab(schedules)
    return {"ok": True, "crontab": sync}


@router.post("/schedules/sync")
async def sync_schedules() -> dict[str, Any]:
    store = _get_store()
    schedules = await store.list_schedules()
    return cron_mod.sync_crontab(schedules)


class BackupVerifierModule:
    name = "backup_verifier"
    version = "0.3.0"
    description = (
        "Compose-Stack-Backups (Voll/tar oder Incremental/restic) mit flexiblen Zielen "
        "(Host → Copilot → SFTP), Verify, Verlauf, Zeitplan und Restore."
    )
    enabled = True
    meta = {
        "phase": 2,
        "role": "backup",
        "ui_path": "/modules/backup_verifier",
        "unit": "compose_stack",
    }

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app: FastAPI) -> None:
        global _store
        bs = get_backup_settings()
        bs.copilot_dir.mkdir(parents=True, exist_ok=True)
        _store = BackupStore(bs.db_path)
        await _store.connect()
        await dest_mod.ensure_seeded(_store, bs)
        app.state.backup_store = _store

        templates = _make_templates()
        settings = get_settings()

        def _page_ctx(active_view: str) -> dict[str, Any]:
            return {
                "app_name": settings.app_name,
                "app_version": settings.app_version,
                "now": format_de(now_berlin()),
                "module_version": self.version,
                "active_view": active_view,
            }

        @app.get("/modules/backup_verifier", response_class=HTMLResponse)
        async def backup_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "page.html",
                _page_ctx("backup"),
            )

        @app.get("/modules/backup_verifier/schedule", response_class=HTMLResponse)
        async def backup_schedule_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "schedule.html",
                _page_ctx("schedule"),
            )

        @app.get("/modules/backup_verifier/history", response_class=HTMLResponse)
        async def backup_history_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "history.html",
                _page_ctx("history"),
            )

        @app.get("/modules/backup_verifier/destinations", response_class=HTMLResponse)
        async def backup_destinations_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "destinations.html",
                _page_ctx("destinations"),
            )

        @app.get("/modules/backup_verifier/browser", response_class=HTMLResponse)
        async def backup_browser_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "browser.html",
                _page_ctx("browser"),
            )

        async def _sched_runner() -> None:
            async def fire(schedule: dict[str, Any]) -> None:
                await _fire_scheduled_backup(app, schedule)

            def snapshot_ready() -> bool:
                topo = getattr(app.state, "topology_store", None)
                return topo is not None and topo.snapshot is not None

            await run_schedule_loop(
                _store,
                fire,
                snapshot_ready=snapshot_ready,
            )

        global _sched_task
        _sched_task = asyncio.create_task(
            _sched_runner(), name="backup-verifier-scheduler"
        )
        app.state.backup_scheduler_task = _sched_task

        logger.info(
            "backup_verifier ready — DB %s, backups %s, scheduler=in_process",
            bs.db_path,
            bs.copilot_dir,
        )

    async def on_shutdown(self, app: FastAPI) -> None:
        global _store, _sched_task
        task = getattr(app.state, "backup_scheduler_task", None) or _sched_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            _sched_task = None
        if _store:
            await _store.close()
            _store = None

    async def on_topology_refresh(self, topology: dict[str, Any]) -> None:
        n = len(topology.get("containers") or [])
        logger.debug("backup_verifier topology refresh: %s containers", n)


MODULE = BackupVerifierModule()
