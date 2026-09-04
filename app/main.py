"""Homelab Operations Copilot — FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api import router as api_router
from app.api.ssh_ws import router as ssh_ws_router
from app.config import get_settings
from app.core.discovery import DiscoveryEngine
from app.core.locale import (
    format_bytes,
    format_de,
    format_uptime,
    metric_level,
    now_berlin,
)
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

    app.state.topology_store = store
    app.state.discovery_engine = engine
    app.state.settings = settings

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

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    app.include_router(api_router)
    app.include_router(ssh_ws_router)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        store: TopologyStore = request.app.state.topology_store
        snap = store.snapshot
        modules = registry.list_modules()
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
