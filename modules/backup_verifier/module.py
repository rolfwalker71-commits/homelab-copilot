"""Smart Backup Integrity Verifier — ModuleProtocol entrypoint."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

# Ensure ``modules/`` is importable as package root for sibling modules.
_MODULES_ROOT = Path(__file__).resolve().parent.parent
if str(_MODULES_ROOT) not in sys.path:
    sys.path.insert(0, str(_MODULES_ROOT))

from app.config import get_settings
from app.core.docker_control import DockerControlError
from app.core.locale import format_bytes, format_de, now_berlin
from app.core.topology import TopologyStore

from backup_verifier.backup import BackupError, list_backup_stacks, run_backup
from backup_verifier.config import get_backup_settings
from backup_verifier import cron as cron_mod
from backup_verifier import destinations as dest_mod
from backup_verifier.inventory import build_inventory
from backup_verifier.jobs import JOBS
from backup_verifier.restore import RestoreError, run_restore
from backup_verifier.store import BackupStore

logger = logging.getLogger(__name__)

router = APIRouter()
_store: BackupStore | None = None


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


class RunPayload(BaseModel):
    parent_id: str = Field(..., min_length=1)
    project: str = Field(..., min_length=1)
    quiesce: bool | None = None


class RestorePayload(BaseModel):
    confirm: bool = False
    # Destination id (int as string), "copilot", "synology", or kind/label
    source: str = Field(default="copilot", min_length=1, max_length=64)


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
        "version": "0.2.0",
        "time": format_de(now_berlin()),
        "copilot_dir": str(bs.copilot_dir),
        "lxc_dir": bs.backup_lxc_dir,
        "keep": {
            "lxc": bs.backup_lxc_keep,
            "copilot": bs.backup_copilot_keep,
            "synology": bs.backup_synology_keep,
        },
        "quiesce_default": bs.backup_quiesce,
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
        try:
            result = await run_backup(
                store,
                parent_id=payload.parent_id,
                project=payload.project,
                snapshot=snap,
                quiesce=payload.quiesce,
            )
            return {"ok": True, "run": result}
        except BackupError as exc:
            raise HTTPException(status_code=409, detail=exc.message) from exc
        except DockerControlError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    job = JOBS.create(parent_id=payload.parent_id, project=payload.project)

    async def _bg() -> None:
        JOBS.set_running(job.id)

        async def on_progress(
            *,
            phase: str,
            percent: int,
            message: str,
            run_id: int | None = None,
        ) -> None:
            JOBS.set_progress(
                job.id,
                phase=phase,
                percent=percent,
                message=message,
                run_id=run_id,
            )

        async def on_log(line: str) -> None:
            JOBS.append_log(job.id, line)

        try:
            result = await run_backup(
                store,
                parent_id=payload.parent_id,
                project=payload.project,
                snapshot=snap,
                quiesce=payload.quiesce,
                on_progress=on_progress,
                on_log=on_log,
            )
            status = str((result or {}).get("status") or "success")
            JOBS.finish(
                job.id,
                status=status if status in ("success", "partial", "failed") else "success",
                result=result,
            )
        except BackupError as exc:
            JOBS.finish(
                job.id,
                status="failed",
                error=exc.message,
                phase="Fehler",
                message=exc.message,
            )
        except DockerControlError as exc:
            JOBS.finish(
                job.id,
                status="failed",
                error=exc.message,
                phase="Fehler",
                message=exc.message,
            )
        except Exception as exc:
            logger.exception("Background backup failed")
            msg = str(exc) or "Unbekannter Fehler beim Backup."
            JOBS.finish(
                job.id,
                status="failed",
                error=msg,
                phase="Fehler",
                message=msg,
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


@router.get("/jobs/{job_id}")
async def get_backup_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden.")
    return {"ok": True, "job": job.to_dict()}


@router.get("/history")
async def history(
    limit: int = Query(50, ge=1, le=200),
    stack: str | None = None,
) -> dict[str, Any]:
    store = _get_store()
    runs = await store.list_runs(limit=limit, stack=stack)
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
async def list_schedules() -> dict[str, Any]:
    store = _get_store()
    schedules = await store.list_schedules()
    return {
        "schedules": schedules,
        "crontab_available": cron_mod.crontab_available(),
        "preview": cron_mod.preview_crontab(schedules),
    }


@router.post("/schedules")
async def save_schedule(payload: SchedulePayload) -> dict[str, Any]:
    store = _get_store()
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

    sid = await store.upsert_schedule(
        schedule_id=payload.id,
        stack=payload.stack,
        parent_id=payload.parent_id,
        cron_expr=expr,
        preset=payload.preset,
        enabled=payload.enabled,
        note=payload.note,
    )
    schedules = await store.list_schedules()
    sync = cron_mod.sync_crontab(schedules)
    return {"ok": True, "id": sid, "cron_expr": expr, "crontab": sync}


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
    version = "0.2.0"
    description = (
        "Compose-Stack-Backups mit flexiblen Zielen (Host-Staging → Copilot → SFTP), "
        "Checksum-Verify, Verlauf und Zeitplan."
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

        logger.info(
            "backup_verifier ready — DB %s, backups %s",
            bs.db_path,
            bs.copilot_dir,
        )

    async def on_shutdown(self, app: FastAPI) -> None:
        global _store
        if _store:
            await _store.close()
            _store = None

    async def on_topology_refresh(self, topology: dict[str, Any]) -> None:
        n = len(topology.get("containers") or [])
        logger.debug("backup_verifier topology refresh: %s containers", n)


MODULE = BackupVerifierModule()
