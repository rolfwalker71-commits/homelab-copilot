"""Ops-Agent — planning board and API (German UI)."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

_MODULES_ROOT = Path(__file__).resolve().parent.parent
if str(_MODULES_ROOT) not in sys.path:
    sys.path.insert(0, str(_MODULES_ROOT))

from app.config import get_settings
from app.core.locale import format_bytes, format_de, now_berlin
from app.core.push import push_allowed, send_push_to_all
from app.core.topology import TopologyStore

from ops_agent.config import get_ops_settings
from ops_agent.engine import OpsEngine, run_ops_loop
from ops_agent.policy import apply_interview, default_policy
from ops_agent.store import OpsStore

logger = logging.getLogger(__name__)

router = APIRouter()
_store: OpsStore | None = None
_engine: OpsEngine | None = None
_loop_task: asyncio.Task[None] | None = None
_scan_task: asyncio.Task[Any] | None = None


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
    env.globals["format_bytes"] = format_bytes
    templates = Jinja2Templates(directory=str(module_dir))
    templates.env = env
    return templates


def _get_engine() -> OpsEngine:
    if _engine is None:
        raise HTTPException(status_code=503, detail="Ops-Agent nicht bereit.")
    return _engine


def _http(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=getattr(exc, "message", None) or str(exc))


class PolicyPayload(BaseModel):
    confirm_kernel_docker: bool = True
    confirm_new_guest_backup: bool = True
    confirm_production: bool = False
    confirm_nothing: bool = False
    production_tags: list[str] = Field(default_factory=lambda: ["prod", "production"])
    focus_mode: str = Field(default="all", pattern="^(all|only|exclude)$")
    focus_ids: list[str] = Field(default_factory=list)
    focus_tags: list[str] = Field(default_factory=list)
    patch_scope_ids: list[str] | None = None
    image_scope_ids: list[str] | None = None
    enabled: bool | None = True


class ScopePayload(BaseModel):
    patch_scope_ids: list[str] = Field(default_factory=list)
    image_scope_ids: list[str] = Field(default_factory=list)


class ScopePromptPayload(BaseModel):
    prompt_id: int = Field(..., ge=1)
    patch: bool | None = None
    image: bool | None = None
    backup: bool | None = None
    drop: bool | None = None


class SettingsPayload(BaseModel):
    enabled: bool | None = None
    shift_auto: bool | None = None
    quiet_start: str | None = None
    quiet_end: str | None = None


class WindowIdPayload(BaseModel):
    window_id: int = Field(..., ge=1)


def _patcher_modules() -> list[Any]:
    """Registry instance first, then the package import — they should be one."""
    seen: list[Any] = []
    for name in ("homelab_modules.patcher", "patcher.module"):
        mod = sys.modules.get(name)
        if mod is not None and mod not in seen:
            seen.append(mod)
    if not seen:
        import patcher.module as pm

        seen.append(pm)
    return seen


async def _open_patcher_store(app: FastAPI) -> Any:
    """Same store `/modules/patcher` opened in PatcherModule.on_startup."""
    store = getattr(app.state, "patcher_store", None)
    if store is None:
        last_exc: Exception | None = None
        for mod in _patcher_modules():
            ensure = getattr(mod, "_ensure_store", None)
            if ensure is None:
                continue
            try:
                store = await ensure(app)
                break
            except Exception as exc:
                last_exc = exc
                store = None
        if store is None and last_exc is not None:
            raise HTTPException(
                status_code=503, detail="Patcher-Store nicht bereit."
            ) from last_exc
    if store is None:
        raise HTTPException(status_code=503, detail="Patcher-Store nicht bereit.")
    app.state.patcher_store = store
    for mod in _patcher_modules():
        bind = getattr(mod, "_bind_store", None)
        if bind is not None:
            try:
                bind(store)
                continue
            except Exception:
                pass
        try:
            mod._store = store
        except Exception:
            pass
    return store


def _scan_status() -> dict[str, Any]:
    try:
        from patcher.scheduler import SCAN_ALL

        return SCAN_ALL.to_dict()
    except Exception:
        return {"running": False, "message": ""}


@router.get("/board")
async def api_board() -> dict[str, Any]:
    data = await _get_engine().board()
    data["scan"] = _scan_status()
    return data


@router.post("/policy")
async def api_save_policy(payload: PolicyPayload) -> dict[str, Any]:
    engine = _get_engine()
    current = await engine.store.get_policy()
    policy = apply_interview(
        current,
        confirm_kernel_docker=payload.confirm_kernel_docker,
        confirm_new_guest_backup=payload.confirm_new_guest_backup,
        confirm_production=payload.confirm_production,
        confirm_nothing=payload.confirm_nothing,
        production_tags=payload.production_tags,
        focus_mode=payload.focus_mode,
        focus_ids=payload.focus_ids,
        focus_tags=payload.focus_tags,
        patch_scope_ids=payload.patch_scope_ids,
        image_scope_ids=payload.image_scope_ids,
    )
    saved = await engine.store.save_policy(policy)
    if payload.enabled is not None:
        await engine.store.save_settings(enabled=bool(payload.enabled), shift_auto=True)
    settings = await engine.settings()
    return {
        "ok": True,
        "policy": saved.to_dict(),
        "enabled": settings.enabled,
        "message": "Agent merkt sich das und arbeitet weiter selbst.",
    }


@router.post("/settings")
async def api_settings(payload: SettingsPayload) -> dict[str, Any]:
    engine = _get_engine()
    await engine.store.save_settings(
        enabled=payload.enabled,
        shift_auto=payload.shift_auto,
        quiet_start=payload.quiet_start,
        quiet_end=payload.quiet_end,
    )
    settings = await engine.settings()
    return {
        "ok": True,
        "enabled": settings.enabled,
        "shift_auto": settings.shift_auto,
        "quiet_start": settings.quiet_start,
        "quiet_end": settings.quiet_end,
    }


@router.post("/scope")
async def api_save_scope(payload: ScopePayload) -> dict[str, Any]:
    engine = _get_engine()
    saved = await engine.save_scope(
        patch_scope_ids=payload.patch_scope_ids,
        image_scope_ids=payload.image_scope_ids,
    )
    return {
        "ok": True,
        "policy": saved.to_dict(),
        "message": "Host-Auswahl gespeichert. Der Agent nutzt nur diese Listen.",
    }


@router.post("/scope-prompt")
async def api_answer_scope_prompt(payload: ScopePromptPayload) -> dict[str, Any]:
    try:
        row = await _get_engine().answer_host_prompt(
            payload.prompt_id,
            patch=payload.patch,
            image=payload.image,
            backup=payload.backup,
            drop=payload.drop,
        )
    except RuntimeError as exc:
        raise _http(exc) from exc
    return {"ok": True, "prompt": row}


@router.post("/plan")
async def api_plan() -> dict[str, Any]:
    engine = _get_engine()
    settings = await engine.settings()
    try:
        result = await engine.propose(auto_apply=True)
    except Exception as exc:
        raise _http(exc) from exc
    result["enabled"] = settings.enabled
    return result


@router.post("/confirm")
async def api_confirm(payload: WindowIdPayload) -> dict[str, Any]:
    try:
        window = await _get_engine().confirm_window(payload.window_id)
    except RuntimeError as exc:
        raise _http(exc) from exc
    return {"ok": True, "window": window}


@router.post("/decline")
async def api_decline(payload: WindowIdPayload) -> dict[str, Any]:
    try:
        window = await _get_engine().decline_window(payload.window_id)
    except RuntimeError as exc:
        raise _http(exc) from exc
    return {"ok": True, "window": window}


@router.post("/start")
async def api_start(payload: WindowIdPayload) -> dict[str, Any]:
    try:
        window = await _get_engine().start_now(payload.window_id)
    except RuntimeError as exc:
        raise _http(exc) from exc
    return {"ok": True, "window": window}


@router.post("/start-now")
async def api_start_accepted() -> dict[str, Any]:
    try:
        return await _get_engine().start_accepted_now()
    except RuntimeError as exc:
        raise _http(exc) from exc


@router.get("/scan-status")
async def api_scan_status() -> dict[str, Any]:
    return {"ok": True, **_scan_status()}


@router.post("/scan-now")
async def api_scan_now(request: Request) -> dict[str, Any]:
    """Start existing patcher scan-all (includes image scan), then ops plan."""
    global _scan_task
    from patcher.scheduler import SCAN_ALL, begin_scan_all

    pm = _patcher_modules()[0]
    await _open_patcher_store(request.app)
    begun = await begin_scan_all(trigger="manual")
    if begun is None:
        raise HTTPException(status_code=409, detail="Scan läuft bereits.")
    engine = _get_engine()
    await engine.log_manual_scan()
    snap = None
    topo = getattr(request.app.state, "topology_store", None)
    if topo is not None:
        snap = getattr(topo, "snapshot", None)

    async def _run() -> None:
        try:
            await pm._run_scan_all(
                snapshot=snap,
                trigger="manual",
                do_summarize=False,
                already_begun=True,
                app=request.app,
            )
        except Exception as exc:
            logger.exception("ops scan-now failed")
            SCAN_ALL.error = str(exc)
            SCAN_ALL.message = f"Scan fehlgeschlagen: {exc}"
            SCAN_ALL.running = False
            try:
                await engine._log_activity(
                    "scan",
                    result="fail",
                    kind="patch",
                    detail=f"Scan fehlgeschlagen: {exc}",
                    via_agent=False,
                )
            except Exception:
                pass

    _scan_task = asyncio.create_task(_run(), name="ops-scan-now")
    return {
        "ok": True,
        "started": True,
        "message": "Scan läuft… Patch- und Image-Prüfung aller Hosts.",
        **SCAN_ALL.to_dict(),
    }


@router.post("/brief")
async def api_refresh_brief() -> dict[str, Any]:
    row = await _get_engine().refresh_evening_brief(force=True)
    return {"ok": True, "brief": row.get("text") or "", "created_at": row.get("created_at")}


class OpsAgentModule:
    name = "ops_agent"
    version = "0.7.0"
    description = (
        "Plant Patch- und Backup-Fenster selbst, prüft Plausibilität, "
        "verschiebt bei Überlauf und startet über die bestehenden Engines."
    )
    enabled = True
    meta = {
        "phase": 3,
        "role": "ops",
        "ui_path": "/ops",
    }

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app: FastAPI) -> None:
        global _store, _engine, _loop_task
        os_ = get_ops_settings()
        _store = OpsStore(os_.db_path)
        await _store.connect()
        app.state.ops_store = _store

        async def _tags(target_id: str) -> list[str]:
            try:
                inv = getattr(app.state, "inventory_store", None)
                if inv is None:
                    return []
                row = await inv.get(target_id)
                return [str(t) for t in (row.get("extra_tags") or []) if t]
            except Exception:
                return []

        def _snap() -> Any:
            topo: TopologyStore | None = getattr(app.state, "topology_store", None)
            return topo.snapshot if topo is not None else None

        def _backup_store() -> Any:
            return getattr(app.state, "backup_store", None)

        async def _stacks(snapshot: Any) -> list[dict[str, Any]]:
            from backup_verifier.backup import list_backup_stacks

            return await list_backup_stacks(snapshot)

        async def _hosts(*_a: Any, **_k: Any) -> list[Any]:
            try:
                from patcher.agent import hosts_from_store as real_hosts

                pstore = await _open_patcher_store(app)
                snap = _snap()
                if pstore is None or snap is None:
                    return []
                return await real_hosts(pstore, snap, tags_for=_tags)
            except Exception:
                logger.exception("Pending Patch/Image-Hosts nicht lesbar")
                return []

        async def _start_backup(window: dict[str, Any]) -> str | None:
            from backup_verifier.module import _enqueue_backup

            store = _backup_store()
            if store is None:
                raise RuntimeError("Backup-Speicher nicht bereit.")
            job, coro = _enqueue_backup(
                store=store,
                parent_id=str(window.get("target_id") or ""),
                project=str(window.get("stack") or ""),
                snapshot=_snap(),
                engine=str(window.get("engine") or "tar"),
                via_agent=True,
            )
            asyncio.create_task(coro(), name=f"ops-backup-{job.id}")
            return job.id

        async def _start_patch(window: dict[str, Any]) -> tuple[bool, str, str | None]:
            wave = getattr(app.state, "patcher_wave_engine", None)
            if wave is None:
                return False, "Wellen-Engine nicht bereit.", None
            return await wave.execute_ops_item(
                target_id=str(window.get("target_id") or ""),
                target_name=str(window.get("target_name") or ""),
                bucket=str(window.get("bucket") or "security"),
                packages=list(window.get("packages") or []),
            )

        def _backup_jobs() -> list[Any]:
            try:
                from backup_verifier.jobs import JOBS

                return JOBS.list_active()
            except Exception:
                return []

        def _patch_jobs() -> list[Any]:
            try:
                from patcher.jobs import JOBS

                return [
                    j
                    for j in JOBS.list_active()
                    if j.kind in ("apply", "image-apply", "apply-batch")
                ]
            except Exception:
                return []

        async def _delete_snap(guest_id: str, snap_name: str) -> Any:
            from patcher.pre_snap import delete_pre_snapshot

            return await delete_pre_snapshot(guest_id, snap_name, via_agent=True)

        async def _rollback_snap(guest_id: str, snap_name: str) -> Any:
            from patcher.pre_snap import rollback_pre_snapshot

            return await rollback_pre_snapshot(guest_id, snap_name, via_agent=True)

        async def _reboot(target_id: str) -> dict[str, Any] | None:
            from patcher.apply import reboot_host
            from patcher.targets import resolve_target

            store = getattr(app.state, "patcher_store", None)
            if store is None:
                try:
                    store = await _open_patcher_store(app)
                except Exception:
                    return None
            if store is None:
                return None
            try:
                target = await resolve_target(store, _snap(), target_id)
            except Exception:
                return None
            return await reboot_host(target, confirm=True)

        async def _prune(target_id: str) -> dict[str, Any] | None:
            from app.core.image_prune import (
                cmd_dangling_image_prune,
                format_unused_image_cleanup_message,
                parse_image_prune_output,
            )
            from patcher.config import get_patcher_settings
            from patcher.sshutil import ssh_run
            from patcher.targets import resolve_target

            store = getattr(app.state, "patcher_store", None)
            if store is None:
                try:
                    store = await _open_patcher_store(app)
                except Exception:
                    return None
            if store is None:
                return None
            try:
                target = await resolve_target(store, _snap(), target_id)
            except Exception:
                return None
            ps = get_patcher_settings()
            stdout, stderr, code = await ssh_run(
                target.ip,
                cmd_dangling_image_prune(),
                timeout=120.0,
                username=target.ssh_user,
                port=target.port,
                connect_timeout=ps.patcher_connect_timeout,
            )
            parsed = parse_image_prune_output(f"{stdout or ''}\n{stderr or ''}")
            warning = None if code == 0 else (stderr or stdout or "Prune fehlgeschlagen")[:180]
            message = format_unused_image_cleanup_message(
                dangling_deleted=int(parsed.get("deleted") or 0),
                dangling_untagged=int(parsed.get("untagged") or 0),
                reclaimed=str(parsed.get("reclaimed") or ""),
                warning=warning,
            )
            return {"ok": code == 0, "message": message, **parsed}

        async def _notify(title: str, body: str, flag: str = "ops_wait", **_k: Any) -> None:
            app_store = getattr(app.state, "app_store", None)
            if app_store is None:
                return
            pref = str(flag or "ops_wait")
            if not await push_allowed(app_store, pref):
                return
            tag = "ops-agent-wait" if pref == "ops_wait" else "ops-agent"
            await send_push_to_all(
                app_store, title=title, body=body, url="/ops", tag=tag
            )

        _engine = OpsEngine(
            _store,
            get_snapshot=_snap,
            get_backup_store=_backup_store,
            list_backup_stacks=_stacks,
            hosts_from_store=_hosts,
            start_backup=_start_backup,
            start_patch=_start_patch,
            list_backup_jobs=_backup_jobs,
            list_patch_jobs=_patch_jobs,
            get_inventory_tags=_tags,
            notify_shift=_notify,
            delete_guest_snap=_delete_snap,
            rollback_guest_snap=_rollback_snap,
            reboot_host=_reboot,
            prune_images=_prune,
        )
        app.state.ops_engine = _engine

        templates = _make_templates()
        settings = get_settings()

        def _page_ctx(active_view: str) -> dict[str, Any]:
            return {
                "app_name": settings.app_name,
                "app_version": settings.app_version,
                "now": format_de(now_berlin()),
                "module_version": self.version,
                "policy_defaults": default_policy().to_dict(),
                "active_view": active_view,
            }

        @app.get("/ops", response_class=HTMLResponse)
        async def ops_board_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "board.html",
                _page_ctx("lage"),
            )

        @app.get("/ops/hosts", response_class=HTMLResponse)
        async def ops_hosts_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "hosts.html",
                _page_ctx("hosts"),
            )

        @app.get("/ops/log", response_class=HTMLResponse)
        async def ops_log_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "log.html",
                _page_ctx("log"),
            )

        @app.get("/ops/regeln", response_class=HTMLResponse)
        async def ops_regeln_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "regeln.html",
                _page_ctx("regeln"),
            )

        _loop_task = asyncio.create_task(run_ops_loop(_engine), name="ops-agent-loop")
        app.state.ops_agent_task = _loop_task
        logger.info("ops_agent ready — DB %s · env_enabled=%s", os_.db_path, os_.ops_agent_enabled)

    async def on_shutdown(self, app: FastAPI) -> None:
        global _store, _engine, _loop_task
        task = getattr(app.state, "ops_agent_task", None) or _loop_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            _loop_task = None
        _engine = None
        if _store:
            await _store.close()
            _store = None


MODULE = OpsAgentModule()
