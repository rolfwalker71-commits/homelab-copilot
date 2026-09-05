"""Homelab Operations Copilot — FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api import router as api_router
from app.api.ssh_ws import router as ssh_ws_router
from app.config import get_settings
from app.core.app_store import AppStore
from app.core.inventory import InventoryStore
from app.core.auth import TotpAuthMiddleware, ensure_totp_secret
from app.core.discovery import DiscoveryEngine
from app.core.backup_storage import bagel_dasharray, copilot_fs_usage
from app.core.host_presence import host_presence_for_app
from app.core.locale import (
    format_bytes,
    format_de,
    format_uptime,
    metric_level,
    now_berlin,
)
from app.core.push import ensure_vapid_keys
from app.core.registry import discover_and_load_modules, registry
from app.core.topology import TopologyStore
from app.core.tree import build_topology_tree

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%d.%m.%Y %H:%M:%S",
)
logger = logging.getLogger("homelab-copilot")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
TEMPLATES.env.globals["format_de"] = format_de
TEMPLATES.env.globals["format_bytes"] = format_bytes
TEMPLATES.env.globals["format_uptime"] = format_uptime
TEMPLATES.env.globals["metric_level"] = metric_level


async def _discovery_loop(app: FastAPI) -> None:
    settings = get_settings()
    engine: DiscoveryEngine = app.state.discovery_engine
    store: TopologyStore = app.state.topology_store
    while True:
        try:
            snapshot = await engine.refresh()
            await store.save(snapshot)
            await store.log(
                "info",
                f"Automatische Discovery: {snapshot.summary['nodes']} Nodes, "
                f"{snapshot.summary['guests']} Guests, "
                f"{snapshot.summary['containers']} Container — {snapshot.refreshed_at}",
            )
            await registry.notify_topology_refresh(snapshot.model_dump())
            logger.info("Discovery refresh OK @ %s", snapshot.refreshed_at)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Discovery loop error")
            try:
                await store.log("error", f"Discovery-Fehler um {format_de(now_berlin())}")
            except Exception:
                pass
        await asyncio.sleep(settings.discovery_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    store = TopologyStore(settings.db_path)
    await store.connect()
    engine = DiscoveryEngine(settings)

    app_store = AppStore(settings.app_db_path)
    await app_store.connect()
    cookie_secret = await app_store.ensure_cookie_secret()
    await ensure_totp_secret(app_store)
    vapid = await ensure_vapid_keys(app_store, settings)

    inventory = InventoryStore(settings.inventory_db_path)
    await inventory.connect()

    app.state.topology_store = store
    app.state.discovery_engine = engine
    app.state.settings = settings
    app.state.app_store = app_store
    app.state.inventory_store = inventory
    app.state.cookie_secret = cookie_secret

    logger.info(
        "Auth/Push bereit — TOTP=%s VAPID-public=%s…",
        "ja" if await app_store.get_totp_secret() else "nein",
        (vapid.get("public_key") or "")[:12],
    )

    # Load drop-in modules (patcher / backup_verifier placeholders skip until ready)
    modules_path = settings.modules_dir
    if not modules_path.is_dir():
        # Dev fallback: repo-local modules/
        modules_path = BASE_DIR.parent / "modules"
    discover_and_load_modules(modules_path)
    registry.mount_routers(app)
    await registry.run_startup(app)

    # Initial discovery (non-blocking start of loop)
    task = asyncio.create_task(_discovery_loop(app), name="discovery-loop")
    app.state.discovery_task = task

    logger.info(
        "%s v%s listening — Zeitbasis Europe/Berlin (%s)",
        settings.app_name,
        settings.app_version,
        format_de(now_berlin()),
    )
    yield

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await registry.run_shutdown(app)
    await inventory.close()
    await app_store.close()
    await store.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )

    app.add_middleware(TotpAuthMiddleware)

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    app.include_router(api_router)
    app.include_router(ssh_ws_router)

    @app.get("/auth/login", response_class=HTMLResponse)
    async def auth_login_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "auth_login.html",
            {
                "app_name": settings.app_name,
                "now": format_de(now_berlin()),
                "next": request.query_params.get("next") or "/",
            },
        )

    @app.get("/auth")
    async def auth_redirect() -> RedirectResponse:
        return RedirectResponse(url="/auth/login", status_code=302)

    def _mobile_page(request: Request, section: str, title: str) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "mobile.html",
            {
                "app_name": settings.app_name,
                "app_version": settings.app_version,
                "now": format_de(now_berlin()),
                "section": section,
                "section_title": title,
            },
        )

    @app.get("/mobile", response_class=HTMLResponse)
    async def mobile_lage(request: Request) -> HTMLResponse:
        return _mobile_page(request, "lage", "Lage")

    @app.get("/mobile/hosts", response_class=HTMLResponse)
    async def mobile_hosts(request: Request) -> HTMLResponse:
        return _mobile_page(request, "hosts", "Hosts")

    @app.get("/mobile/hinweise", response_class=HTMLResponse)
    async def mobile_hinweise(request: Request) -> HTMLResponse:
        return _mobile_page(request, "hinweise", "Hinweise")

    @app.get("/mobile/sichern", response_class=HTMLResponse)
    async def mobile_sichern(request: Request) -> HTMLResponse:
        return _mobile_page(request, "sichern", "Sichern")

    @app.get("/mobile/mehr", response_class=HTMLResponse)
    async def mobile_mehr(request: Request) -> HTMLResponse:
        return _mobile_page(request, "mehr", "Mehr")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        store: TopologyStore = request.app.state.topology_store
        snap = store.snapshot
        modules = registry.list_modules()
        try:
            backup_copilot = copilot_fs_usage()
        except Exception:
            logger.exception("Copilot-Speicher KPI")
            backup_copilot = {}
        try:
            host_kpi = await host_presence_for_app(
                snap,
                patcher_store=getattr(request.app.state, "patcher_store", None),
                health_store=getattr(request.app.state, "health_store", None),
            )
        except Exception:
            logger.exception("Hosts-KPI")
            host_kpi = {
                "online": 0,
                "offline": 0,
                "unmonitored": 0,
                "total": 0,
                "center": "0 / 0",
                "warn": False,
                "online_dash": "0.00 95.19",
                "offline_dash": "0.00 95.19",
                "offline_offset": "0.00",
            }
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "app_name": settings.app_name,
                "app_version": settings.app_version,
                "snapshot": snap,
                "topology": build_topology_tree(snap),
                "modules": modules,
                "proxmox_configured": settings.proxmox_configured,
                "now": format_de(now_berlin()),
                "backup_copilot": backup_copilot,
                "bagel_dasharray": bagel_dasharray,
                "host_kpi": host_kpi,
            },
        )

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "setup.html",
            {
                "app_name": settings.app_name,
                "settings": get_settings(),
                "modules": registry.list_modules(),
                "now": format_de(now_berlin()),
            },
        )

    @app.get("/offline", response_class=HTMLResponse)
    async def offline_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "offline.html",
            {
                "app_name": settings.app_name,
                "now": format_de(now_berlin()),
            },
        )

    @app.get("/sw.js")
    async def service_worker() -> FileResponse:
        """Serve SW from root so scope covers the whole app."""
        return FileResponse(
            BASE_DIR / "static" / "sw.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.get("/manifest.webmanifest")
    async def manifest_alias() -> FileResponse:
        return FileResponse(
            BASE_DIR / "static" / "manifest.webmanifest",
            media_type="application/manifest+json",
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host=s.host,
        port=s.port,
        reload=s.debug,
    )
