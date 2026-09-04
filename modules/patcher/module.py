"""AI-Driven Patch-Management — ModuleProtocol entrypoint."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

_MODULES_ROOT = Path(__file__).resolve().parent.parent
if str(_MODULES_ROOT) not in sys.path:
    sys.path.insert(0, str(_MODULES_ROOT))

from app.config import get_settings
from app.core.docker_control import DockerControlError, ssh_key_present
from app.core.locale import format_de, now_berlin
from app.core.topology import TopologyStore

from patcher.apply import ApplyError, apply_updates, reboot_host
from patcher.config import get_patcher_settings
from patcher import cron as cron_mod
from patcher.jobs import JOBS
from patcher.llm import LlmError, summarize_scan
from patcher.scan import ScanError, scan_target
from patcher.scheduler import SCAN_ALL, begin_scan_all, daily_scan_loop, run_scan_all_hosts
from patcher.sshutil import ssh_probe
from patcher.store import PatcherStore
from patcher.targets import list_targets, resolve_target

logger = logging.getLogger(__name__)

router = APIRouter()
_store: PatcherStore | None = None
_daily_task: Any = None


def _make_templates() -> Jinja2Templates:
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
    templates = Jinja2Templates(directory=str(module_dir))
    templates.env = env
    return templates


def _get_store() -> PatcherStore:
    if _store is None:
        raise HTTPException(status_code=503, detail="Patcher-Store nicht bereit.")
    return _store


def _snapshot(request: Request):
    store: TopologyStore = request.app.state.topology_store
    return store.snapshot


def _http_docker(exc: DockerControlError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


# --- payloads ---


class ScanPayload(BaseModel):
    target_id: str = Field(..., min_length=1)
    wait: bool = False
    summarize: bool = True


class ApplyPayload(BaseModel):
    target_id: str = Field(..., min_length=1)
    confirm: bool = False
    package_filter: str = Field(default="all", pattern="^(security|all|selected)$")
    packages: list[str] = Field(default_factory=list)
    wait: bool = False


class RebootPayload(BaseModel):
    target_id: str = Field(..., min_length=1)
    confirm: bool = False


class HostPayload(BaseModel):
    id: int | None = None
    name: str = Field(..., min_length=1, max_length=128)
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, max_length=64)
    enabled: bool = True
    note: str = Field(default="", max_length=500)


class HostCheckPayload(BaseModel):
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, max_length=64)


class SchedulePayload(BaseModel):
    id: int | None = None
    target_id: str = Field(..., min_length=1)
    preset: str = Field(default="daily", pattern="^(daily|weekly|custom)$")
    time: str = Field(default="04:00")
    weekday: int = Field(default=0, ge=0, le=6)
    cron_expr: str | None = None
    enabled: bool = True
    note: str = ""


class SummarizePayload(BaseModel):
    scan_id: int


# --- helpers ---


async def _enrich_targets(store: PatcherStore, snapshot) -> list[dict[str, Any]]:
    targets = await list_targets(store, snapshot)
    out: list[dict[str, Any]] = []
    for t in targets:
        d = t.to_dict()
        latest = await store.latest_scan_for_target(t.id)
        if latest and latest.get("status") == "success":
            d["last_scan"] = {
                "id": latest["id"],
                "created_at": latest.get("created_at"),
                "pm": latest.get("pm"),
                "distro": latest.get("distro"),
                "summary": latest.get("summary") or {},
                "llm_summary": latest.get("llm_summary"),
                "reboot_required": latest.get("reboot_required"),
            }
            d["pending"] = (latest.get("summary") or {}).get("total", 0)
            d["security"] = (latest.get("summary") or {}).get("security", 0)
        else:
            d["last_scan"] = None
            d["pending"] = None
            d["security"] = None
            if latest:
                d["last_scan_error"] = latest.get("error_message")
        out.append(d)
    return out


async def _run_scan_job(
    job_id: str,
    *,
    target_id: str,
    snapshot,
    do_summarize: bool,
) -> None:
    store = _store
    if store is None:
        JOBS.finish(job_id, status="failed", error="Store nicht bereit")
        return {"status": "failed", "scan_id": None, "summary": {}, "error": "Store nicht bereit"}
    scan_id: int | None = None
    try:
        target = await resolve_target(store, snapshot, target_id)
        JOBS.set_progress(
            job_id,
            phase="Start",
            percent=5,
            message=f"Scan für {target.name} wird vorbereitet…",
        )
        scan_id = await store.create_scan(
            target_id=target.id, target_name=target.name, status="running"
        )
        JOBS.set_progress(job_id, scan_id=scan_id)

        async def on_progress(phase: str, percent: int, message: str) -> None:
            JOBS.set_progress(job_id, phase=phase, percent=percent, message=message)
            JOBS.append_log(job_id, f"{phase}: {message}")

        result = await scan_target(target, progress=on_progress)
        llm_text = None
        if do_summarize and get_patcher_settings().llm_configured:
            JOBS.set_progress(
                job_id,
                phase="KI",
                percent=90,
                message="KI-Zusammenfassung wird erstellt…",
            )
            JOBS.append_log(job_id, "LLM-Zusammenfassung…")
            try:
                llm_text = await summarize_scan(
                    target_name=target.name,
                    distro=result.get("distro"),
                    pm=result.get("pm"),
                    summary=result.get("summary") or {},
                    packages=result.get("packages") or [],
                )
            except LlmError as exc:
                JOBS.append_log(job_id, f"LLM übersprungen: {exc.message}")
        elif do_summarize:
            JOBS.append_log(job_id, "Kein LLM-Key — nur Heuristik")

        await store.finish_scan(
            scan_id,
            status="success",
            pm=result.get("pm"),
            distro=result.get("distro"),
            summary=result.get("summary"),
            llm_summary=llm_text,
            reboot_required=bool(result.get("reboot_required")),
            packages=result.get("packages") or [],
        )
        summary = result.get("summary") or {}
        JOBS.finish(
            job_id,
            status="success",
            result={
                "scan_id": scan_id,
                "summary": summary,
                "pm": result.get("pm"),
                "distro": result.get("distro"),
                "reboot_required": result.get("reboot_required"),
                "llm_summary": llm_text,
            },
            message=(
                f"{summary.get('total', 0)} Updates "
                f"({summary.get('security', 0)} Security)"
            ),
            phase="Fertig",
        )
        return {
            "status": "success",
            "scan_id": scan_id,
            "summary": summary,
            "error": None,
        }
    except (ScanError, DockerControlError, RuntimeError) as exc:
        msg = getattr(exc, "message", None) or str(exc)
        if scan_id is not None:
            try:
                await store.finish_scan(scan_id, status="failed", error_message=msg)
            except Exception:
                logger.exception("finish_scan failed")
        JOBS.finish(job_id, status="failed", error=msg)
        return {"status": "failed", "scan_id": scan_id, "summary": {}, "error": msg}
    except Exception as exc:
        logger.exception("scan job failed")
        if scan_id is not None:
            try:
                await store.finish_scan(scan_id, status="failed", error_message=str(exc))
            except Exception:
                logger.exception("finish_scan failed")
        JOBS.finish(job_id, status="failed", error=str(exc))
        return {"status": "failed", "scan_id": scan_id, "summary": {}, "error": str(exc)}


async def _notify_scan_findings(summary: dict[str, Any]) -> None:
    """Push when daily/manual all-host scan finds updates or errors."""
    hosts_u = int(summary.get("hosts_with_updates") or 0)
    hosts_e = int(summary.get("hosts_with_errors") or 0)
    total = int(summary.get("total_updates") or 0)
    security = int(summary.get("total_security") or 0)
    if hosts_u <= 0 and hosts_e <= 0:
        return
    try:
        from app.core.push import send_push_to_all

        # app.state is set on the FastAPI app; pull store via a soft global if needed
        store = None
        try:
            from app.main import app as fastapi_app

            store = getattr(fastapi_app.state, "app_store", None)
        except Exception:
            store = None
        if store is None:
            return
        parts = []
        if hosts_u:
            parts.append(
                f"{hosts_u} Host(s) mit {total} Update(s) ({security} Security)"
            )
        if hosts_e:
            parts.append(f"{hosts_e} Host(s) mit Fehlern")
        body = " · ".join(parts)
        await send_push_to_all(
            store,
            title="HomelabOps — Patch-Check",
            body=body,
            url="/modules/patcher",
            tag="patcher-scan",
        )
    except Exception:
        logger.exception("Push-Benachrichtigung fehlgeschlagen")


async def _scan_one_target_sync(target, *, snapshot, do_summarize: bool = False) -> dict[str, Any]:
    """Run a single-host scan synchronously (for sequential scan-all)."""
    job = JOBS.create(kind="scan", target_id=target.id)
    result = await _run_scan_job(
        job.id,
        target_id=target.id,
        snapshot=snapshot,
        do_summarize=do_summarize,
    )
    return result or {"status": "failed", "summary": {}, "error": "unbekannt"}


async def _run_scan_all(
    *,
    snapshot,
    trigger: str,
    do_summarize: bool = False,
    already_begun: bool = False,
) -> dict[str, Any]:
    store = _store
    if store is None:
        SCAN_ALL.running = False
        return {"ok": False, "error": "Store nicht bereit."}
    targets = await list_targets(store, snapshot)

    async def scan_one(target):
        return await _scan_one_target_sync(
            target, snapshot=snapshot, do_summarize=do_summarize
        )

    async def on_complete(summary: dict[str, Any]) -> None:
        try:
            from app.main import app as fastapi_app

            app_store = getattr(fastapi_app.state, "app_store", None)
            if app_store is not None:
                await app_store.set_patcher_last_daily(
                    summary.get("finished_at_iso") or ""
                )
        except Exception:
            logger.exception("patcher last_daily speichern fehlgeschlagen")
        await _notify_scan_findings(summary)

    return await run_scan_all_hosts(
        targets=targets,
        scan_one=scan_one,
        trigger=trigger,
        on_complete=on_complete,
        already_begun=already_begun,
    )


async def _run_apply_job(
    job_id: str,
    *,
    target_id: str,
    snapshot,
    package_filter: str,
    packages: list[str],
) -> None:
    store = _store
    if store is None:
        JOBS.finish(job_id, status="failed", error="Store nicht bereit")
        return
    apply_id: int | None = None
    try:
        target = await resolve_target(store, snapshot, target_id)
        JOBS.set_progress(
            job_id,
            phase="Start",
            percent=5,
            message=f"Apply für {target.name} wird vorbereitet…",
        )
        apply_id = await store.create_apply_run(
            target_id=target.id,
            target_name=target.name,
            package_filter=package_filter,
            packages=packages,
        )
        JOBS.set_progress(
            job_id,
            apply_id=apply_id,
            phase="SSH",
            percent=20,
            message=f"Verbinde zu {target.ip} und spiele Updates ein ({package_filter})…",
        )
        JOBS.append_log(
            job_id,
            f"Apply {package_filter} auf {target.name} ({target.ip})",
        )
        result = await apply_updates(
            target, package_filter=package_filter, packages=packages
        )
        JOBS.set_progress(
            job_id,
            phase="Abschluss",
            percent=90,
            message="Ergebnis speichern…",
        )
        await store.finish_apply_run(
            apply_id,
            status="success",
            log_text=result.get("log"),
            reboot_required=bool(result.get("reboot_required")),
            pm=result.get("pm"),
        )
        msg = "Updates eingespielt."
        if result.get("reboot_required"):
            msg += " Reboot empfohlen."
        JOBS.finish(
            job_id,
            status="success",
            result={"apply_id": apply_id, **result},
            message=msg,
        )
    except (ApplyError, DockerControlError) as exc:
        msg = getattr(exc, "message", None) or str(exc)
        if apply_id is not None:
            await store.finish_apply_run(
                apply_id, status="failed", error_message=msg
            )
        JOBS.finish(job_id, status="failed", error=msg)
    except Exception as exc:
        logger.exception("apply job failed")
        if apply_id is not None:
            await store.finish_apply_run(
                apply_id, status="failed", error_message=str(exc)
            )
        JOBS.finish(job_id, status="failed", error=str(exc))


# --- API ---


@router.get("/status")
async def module_status(request: Request) -> dict[str, Any]:
    ps = get_patcher_settings()
    s = get_settings()
    store = _get_store()
    snap = _snapshot(request)
    targets = await list_targets(store, snap)
    return {
        "module": "patcher",
        "version": "0.1.0",
        "time": format_de(now_berlin()),
        "ssh_key_present": ssh_key_present(s),
        "llm_configured": ps.llm_configured,
        "llm_model": ps.patcher_llm_model if ps.llm_configured else None,
        "target_count": len(targets),
        "crontab": cron_mod.crontab_available(),
    }


@router.get("/targets")
async def api_targets(request: Request) -> dict[str, Any]:
    store = _get_store()
    items = await _enrich_targets(store, _snapshot(request))
    return {"targets": items, "count": len(items)}


@router.get("/scans/{scan_id}")
async def api_scan_detail(scan_id: int) -> dict[str, Any]:
    store = _get_store()
    scan = await store.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan nicht gefunden.")
    packages = await store.list_packages(scan_id)
    return {"scan": scan, "packages": packages}


@router.get("/history")
async def api_history(
    limit: int = Query(default=40, ge=1, le=200),
) -> dict[str, Any]:
    store = _get_store()
    scans = await store.list_scans(limit=limit)
    applies = await store.list_apply_runs(limit=limit)
    return {"scans": scans, "apply_runs": applies}


@router.post("/scan")
async def api_scan(
    payload: ScanPayload,
    request: Request,
    background: BackgroundTasks,
) -> dict[str, Any]:
    store = _get_store()
    snap = _snapshot(request)
    try:
        target = await resolve_target(store, snap, payload.target_id)
    except DockerControlError as exc:
        raise _http_docker(exc) from exc

    job = JOBS.create(kind="scan", target_id=target.id)
    if payload.wait:
        await _run_scan_job(
            job.id,
            target_id=target.id,
            snapshot=snap,
            do_summarize=payload.summarize,
        )
        done = JOBS.get(job.id)
        return done.to_dict() if done else {"job_id": job.id, "status": "unknown"}

    background.add_task(
        _run_scan_job,
        job.id,
        target_id=target.id,
        snapshot=snap,
        do_summarize=payload.summarize,
    )
    return job.to_dict()


@router.get("/scan-all/status")
async def api_scan_all_status() -> dict[str, Any]:
    ps = get_patcher_settings()
    return {
        **SCAN_ALL.to_dict(),
        "daily_enabled": ps.patcher_daily_enabled,
        "daily_hour": ps.patcher_daily_hour,
        "daily_cron": ps.patcher_cron or None,
    }


@router.post("/scan-all")
async def api_scan_all(
    request: Request,
    background: BackgroundTasks,
    wait: bool = False,
) -> dict[str, Any]:
    """Sequentially scan all patch targets (manual „Alle Hosts prüfen“)."""
    begun = await begin_scan_all(trigger="manual")
    if begun is None:
        raise HTTPException(status_code=409, detail="Scan läuft bereits.")
    snap = _snapshot(request)
    if wait:
        return await _run_scan_all(
            snapshot=snap, trigger="manual", do_summarize=False, already_begun=True
        )
    background.add_task(
        _run_scan_all,
        snapshot=snap,
        trigger="manual",
        do_summarize=False,
        already_begun=True,
    )
    return {"ok": True, "started": True, **SCAN_ALL.to_dict()}


@router.post("/apply")
async def api_apply(
    payload: ApplyPayload,
    request: Request,
    background: BackgroundTasks,
) -> dict[str, Any]:
    if not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail="Apply erfordert confirm=true.",
        )
    store = _get_store()
    snap = _snapshot(request)
    try:
        target = await resolve_target(store, snap, payload.target_id)
    except DockerControlError as exc:
        raise _http_docker(exc) from exc

    job = JOBS.create(kind="apply", target_id=target.id)
    if payload.wait:
        await _run_apply_job(
            job.id,
            target_id=target.id,
            snapshot=snap,
            package_filter=payload.package_filter,
            packages=payload.packages,
        )
        done = JOBS.get(job.id)
        return done.to_dict() if done else {"job_id": job.id, "status": "unknown"}

    background.add_task(
        _run_apply_job,
        job.id,
        target_id=target.id,
        snapshot=snap,
        package_filter=payload.package_filter,
        packages=payload.packages,
    )
    return job.to_dict()


@router.post("/reboot")
async def api_reboot(payload: RebootPayload, request: Request) -> dict[str, Any]:
    store = _get_store()
    try:
        target = await resolve_target(store, _snapshot(request), payload.target_id)
        result = await reboot_host(target, confirm=payload.confirm)
    except (ApplyError, DockerControlError) as exc:
        msg = getattr(exc, "message", None) or str(exc)
        code = getattr(exc, "status_code", 400)
        raise HTTPException(status_code=code, detail=msg) from exc
    return result


@router.post("/summarize")
async def api_summarize(payload: SummarizePayload) -> dict[str, Any]:
    store = _get_store()
    scan = await store.get_scan(payload.scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan nicht gefunden.")
    packages = await store.list_packages(payload.scan_id)
    try:
        text = await summarize_scan(
            target_name=scan.get("target_name") or scan.get("target_id"),
            distro=scan.get("distro"),
            pm=scan.get("pm"),
            summary=scan.get("summary") or {},
            packages=packages,
        )
    except LlmError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    await store.set_scan_llm_summary(payload.scan_id, text)
    return {"ok": True, "scan_id": payload.scan_id, "llm_summary": text}


@router.get("/jobs/{job_id}")
async def api_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden.")
    return job.to_dict()


# --- hosts CRUD ---


@router.get("/hosts")
async def api_list_hosts() -> dict[str, Any]:
    store = _get_store()
    return {"hosts": await store.list_hosts()}


@router.post("/hosts")
async def api_upsert_host(payload: HostPayload) -> dict[str, Any]:
    store = _get_store()
    hid = await store.upsert_host(
        host_id=payload.id,
        name=payload.name.strip(),
        host=payload.host.strip(),
        port=payload.port,
        ssh_user=payload.ssh_user,
        enabled=payload.enabled,
        note=payload.note,
    )
    row = await store.get_host(hid)
    return {"ok": True, "host": row}


@router.delete("/hosts/{host_id}")
async def api_delete_host(host_id: int) -> dict[str, Any]:
    store = _get_store()
    await store.delete_host(host_id)
    return {"ok": True}


@router.post("/hosts/check")
async def api_check_host(payload: HostCheckPayload) -> dict[str, Any]:
    ps = get_patcher_settings()
    try:
        result = await ssh_probe(
            payload.host.strip(),
            username=payload.ssh_user,
            port=payload.port,
            connect_timeout=ps.patcher_connect_timeout,
        )
    except DockerControlError as exc:
        raise _http_docker(exc) from exc
    return result


# --- schedules ---


@router.get("/schedules")
async def api_list_schedules() -> dict[str, Any]:
    store = _get_store()
    return {
        "schedules": await store.list_schedules(),
        "crontab_available": cron_mod.crontab_available(),
    }


@router.post("/schedules")
async def api_upsert_schedule(payload: SchedulePayload) -> dict[str, Any]:
    store = _get_store()
    try:
        if payload.preset == "custom":
            if not payload.cron_expr:
                raise cron_mod.CronError("cron_expr erforderlich bei preset=custom")
            expr = cron_mod.validate_cron_expr(payload.cron_expr)
        else:
            expr = cron_mod.preset_to_cron(
                payload.preset, payload.time, payload.weekday
            )
    except cron_mod.CronError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    sid = await store.upsert_schedule(
        schedule_id=payload.id,
        target_id=payload.target_id,
        cron_expr=expr,
        preset=payload.preset,
        enabled=payload.enabled,
        note=payload.note,
    )
    schedules = await store.list_schedules()
    sync = cron_mod.sync_crontab(schedules)
    return {"ok": True, "id": sid, "cron_expr": expr, "crontab": sync}


@router.delete("/schedules/{schedule_id}")
async def api_delete_schedule(schedule_id: int) -> dict[str, Any]:
    store = _get_store()
    await store.delete_schedule(schedule_id)
    schedules = await store.list_schedules()
    sync = cron_mod.sync_crontab(schedules)
    return {"ok": True, "crontab": sync}


class PatcherModule:
    name = "patcher"
    version = "0.1.0"
    description = (
        "AI-Driven Patch-Management: Updates auf Proxmox-Guests und manuellen "
        "Linux-Hosts scannen, melden und mit Bestätigung einspielen (apt/dnf/apk)."
    )
    enabled = True
    meta = {
        "phase": 2,
        "role": "patch",
        "ui_path": "/modules/patcher",
    }

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app: FastAPI) -> None:
        global _store, _daily_task
        ps = get_patcher_settings()
        _store = PatcherStore(ps.db_path)
        await _store.connect()
        app.state.patcher_store = _store

        templates = _make_templates()
        settings = get_settings()

        def _page_ctx(active_view: str) -> dict[str, Any]:
            return {
                "app_name": settings.app_name,
                "app_version": settings.app_version,
                "now": format_de(now_berlin()),
                "module_version": self.version,
                "active_view": active_view,
                "llm_configured": ps.llm_configured,
            }

        @app.get("/modules/patcher", response_class=HTMLResponse)
        async def patcher_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "page.html",
                _page_ctx("overview"),
            )

        @app.get("/modules/patcher/hosts", response_class=HTMLResponse)
        async def patcher_hosts_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "hosts.html",
                _page_ctx("hosts"),
            )

        @app.get("/modules/patcher/history", response_class=HTMLResponse)
        async def patcher_history_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "history.html",
                _page_ctx("history"),
            )

        @app.get("/modules/patcher/schedule", response_class=HTMLResponse)
        async def patcher_schedule_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "schedule.html",
                _page_ctx("schedule"),
            )

        async def _daily_runner() -> None:
            async def get_targets_and_scan() -> None:
                store: TopologyStore = app.state.topology_store
                snap = store.snapshot
                logger.info("Patcher Daily-Scan startet…")
                await _run_scan_all(
                    snapshot=snap, trigger="daily", do_summarize=False
                )

            await daily_scan_loop(
                enabled=ps.patcher_daily_enabled,
                cron_expr=ps.patcher_cron,
                hour=ps.patcher_daily_hour,
                get_targets_and_scan=get_targets_and_scan,
            )

        if ps.patcher_daily_enabled:
            _daily_task = asyncio.create_task(
                _daily_runner(), name="patcher-daily-scan"
            )
            app.state.patcher_daily_task = _daily_task

        logger.info(
            "patcher ready — DB %s · daily=%s hour=%s cron=%r",
            ps.db_path,
            ps.patcher_daily_enabled,
            ps.patcher_daily_hour,
            ps.patcher_cron or "",
        )

    async def on_shutdown(self, app: FastAPI) -> None:
        global _store, _daily_task
        task = getattr(app.state, "patcher_daily_task", None) or _daily_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            _daily_task = None
        if _store:
            await _store.close()
            _store = None

    async def on_topology_refresh(self, topology: dict[str, Any]) -> None:
        n = len(topology.get("guests") or [])
        logger.debug("patcher topology refresh: %s guests", n)


MODULE = PatcherModule()
