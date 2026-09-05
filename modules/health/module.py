"""Health-Checks + TLS — ModuleProtocol entrypoint."""

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
from app.core.locale import format_de, now_berlin
from app.core.app_store import DEFAULT_PUSH_PREFS

from health.checker import suggest_urls_from_topology
from health.config import get_health_settings
from health.scheduler import (
    health_loop,
    poll_all_checks,
    poll_disk_alerts,
    poll_storage_health,
    run_one_check,
)
from health.store import HealthStore

logger = logging.getLogger(__name__)

router = APIRouter()
_store: HealthStore | None = None
_poll_task: asyncio.Task[None] | None = None


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


def _get_store() -> HealthStore:
    if _store is None:
        raise HTTPException(status_code=503, detail="Health-Store nicht bereit.")
    return _store


class CheckPayload(BaseModel):
    id: int | None = None
    label: str = Field(..., min_length=1, max_length=128)
    url: str = Field(..., min_length=8, max_length=500)
    enabled: bool = True


def _validate_url(url: str) -> str:
    text = url.strip()
    if not (text.startswith("http://") or text.startswith("https://")):
        raise HTTPException(status_code=400, detail="URL muss mit http:// oder https:// beginnen.")
    return text


@router.get("/status")
async def module_status() -> dict[str, Any]:
    hs = get_health_settings()
    store = _get_store()
    checks = await store.list_checks()
    down = sum(1 for c in checks if c.get("last_status") == "down")
    return {
        "module": "health",
        "version": "0.1.0",
        "time": format_de(now_berlin()),
        "poll_interval_seconds": hs.health_poll_interval_seconds,
        "check_count": len(checks),
        "down": down,
    }


@router.get("/checks")
async def api_list_checks() -> dict[str, Any]:
    return {"checks": await _get_store().list_checks()}


@router.post("/checks")
async def api_upsert_check(payload: CheckPayload) -> dict[str, Any]:
    store = _get_store()
    cid = await store.upsert_check(
        check_id=payload.id,
        label=payload.label.strip(),
        url=_validate_url(payload.url),
        enabled=payload.enabled,
        source="manual",
    )
    row = await store.get_check(cid)
    return {"ok": True, "check": row}


@router.delete("/checks/{check_id}")
async def api_delete_check(check_id: int) -> dict[str, Any]:
    await _get_store().delete_check(check_id)
    return {"ok": True}


@router.post("/checks/{check_id}/run")
async def api_run_check(check_id: int) -> dict[str, Any]:
    store = _get_store()
    row = await store.get_check(check_id)
    if not row:
        raise HTTPException(status_code=404, detail="Check nicht gefunden.")
    updated = await run_one_check(store, row)
    return {"ok": True, "check": updated}


@router.post("/run-all")
async def api_run_all(request: Request) -> dict[str, Any]:
    store = _get_store()
    summary = await poll_all_checks(store)
    snap = getattr(request.app.state.topology_store, "snapshot", None)
    await poll_disk_alerts(store, snap.model_dump() if snap else None)
    await poll_storage_health(store)
    return {"ok": True, **summary, "checks": await store.list_checks()}


@router.get("/storage")
async def api_storage_health(request: Request) -> dict[str, Any]:
    store = _get_store()
    snap = getattr(request.app.state.topology_store, "snapshot", None)
    engine = getattr(request.app.state, "discovery_engine", None)
    nodes: list[dict[str, Any]] = []
    names: list[str] = []
    if snap is not None:
        for n in snap.nodes or []:
            name = getattr(n, "name", None)
            if name:
                names.append(str(name))
    for name in names:
        if engine is None:
            continue
        try:
            data = await engine.fetch_node_storage_health(name)
            data = await store.attach_projections(name, data)
            nodes.append(data)
        except Exception as exc:
            nodes.append({"node": name, "error": str(exc)})
    return {"ok": True, "nodes": nodes, "time": format_de(now_berlin())}


@router.get("/suggestions")
async def api_suggestions(request: Request) -> dict[str, Any]:
    snap = getattr(request.app.state.topology_store, "snapshot", None)
    items = suggest_urls_from_topology(snap.model_dump() if snap else {})
    existing = {c["url"] for c in await _get_store().list_checks()}
    for item in items:
        item["exists"] = item["url"] in existing
    return {"suggestions": items}


class HealthModule:
    name = "health"
    version = "0.1.0"
    description = (
        "Erreichbarkeit und TLS-Zertifikate prüfen — Push bei Ausfall "
        "(Zustandswechsel) und bei Ablauf in ≤14 Tagen."
    )
    enabled = True
    meta = {
        "phase": 3,
        "role": "health",
        "ui_path": "/modules/health",
    }

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app: FastAPI) -> None:
        global _store, _poll_task
        hs = get_health_settings()
        _store = HealthStore(hs.db_path)
        await _store.connect()
        app.state.health_store = _store

        templates = _make_templates()
        settings = get_settings()

        @app.get("/modules/health", response_class=HTMLResponse)
        async def health_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "page.html",
                {
                    "app_name": settings.app_name,
                    "app_version": settings.app_version,
                    "now": format_de(now_berlin()),
                    "module_version": self.version,
                    "poll_interval": hs.health_poll_interval_seconds,
                    "push_defaults": DEFAULT_PUSH_PREFS,
                },
            )

        _poll_task = asyncio.create_task(health_loop(_store), name="health-poll")
        app.state.health_poll_task = _poll_task
        logger.info(
            "health ready — DB %s · poll=%ss",
            hs.db_path,
            hs.health_poll_interval_seconds,
        )

    async def on_shutdown(self, app: FastAPI) -> None:
        global _store, _poll_task
        task = getattr(app.state, "health_poll_task", None) or _poll_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            _poll_task = None
        if _store:
            await _store.close()
            _store = None

    async def on_topology_refresh(self, topology: dict[str, Any]) -> None:
        if _store is None:
            return
        suggestions = suggest_urls_from_topology(topology)
        added = 0
        for item in suggestions:
            existing = await _store.find_by_url(item["url"])
            if existing:
                continue
            await _store.upsert_check(
                check_id=None,
                label=item["label"],
                url=item["url"],
                enabled=True,
                source="auto",
            )
            added += 1
        if added:
            logger.info("Health: %d URL(s) aus Topologie übernommen", added)


MODULE = HealthModule()
