"""API routes: health, topology, discovery control, setup."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.core.locale import format_de, now_berlin
from app.core.registry import registry

router = APIRouter(prefix="/api")


class SetupPayload(BaseModel):
    """Persisted via env file rewrite is out of scope; this updates runtime settings."""

    proxmox_host: str = ""
    proxmox_port: int = 8006
    proxmox_user: str = "root@pam"
    proxmox_token_id: str = ""
    proxmox_token_secret: str = ""
    proxmox_password: str = ""
    proxmox_verify_ssl: bool = False
    docker_use_local_socket: bool = True
    docker_ssh_user: str = "root"


@router.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "time": format_de(now_berlin()),
        "proxmox_configured": settings.proxmox_configured,
    }


@router.get("/topology")
async def get_topology(request: Request) -> dict[str, Any]:
    store = request.app.state.topology_store
    snap = store.snapshot
    if snap is None:
        return store.empty_snapshot(
            proxmox_configured=get_settings().proxmox_configured,
            errors=["Noch keine Discovery durchgeführt."],
        ).model_dump()
    data = snap.model_dump()
    data["summary"] = snap.summary
    return data


@router.post("/discovery/refresh")
async def trigger_refresh(request: Request) -> dict[str, Any]:
    engine = request.app.state.discovery_engine
    store = request.app.state.topology_store
    snapshot = await engine.refresh()
    await store.save(snapshot)
    await store.log("info", f"Manuelle Discovery abgeschlossen ({format_de()}).")
    await registry.notify_topology_refresh(snapshot.model_dump())
    data = snapshot.model_dump()
    data["summary"] = snapshot.summary
    return data


@router.get("/discovery/logs")
async def discovery_logs(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    store = request.app.state.topology_store
    return await store.recent_logs(limit=min(limit, 200))


@router.get("/modules")
async def list_modules() -> list[dict[str, Any]]:
    return [
        {
            "name": m.name,
            "version": m.version,
            "description": m.description,
            "enabled": m.enabled,
            "has_router": m.router is not None,
            "meta": m.meta,
        }
        for m in registry.list_modules()
    ]


@router.get("/setup/status")
async def setup_status() -> dict[str, Any]:
    s = get_settings()
    return {
        "proxmox_configured": s.proxmox_configured,
        "proxmox_host": s.proxmox_host,
        "proxmox_port": s.proxmox_port,
        "proxmox_user": s.proxmox_user,
        "proxmox_token_id": s.proxmox_token_id,
        "has_token_secret": bool(s.proxmox_token_secret),
        "has_password": bool(s.proxmox_password),
        "proxmox_verify_ssl": s.proxmox_verify_ssl,
        "docker_use_local_socket": s.docker_use_local_socket,
        "docker_ssh_user": s.docker_ssh_user,
        "docker_ssh_key_present": bool(
            s.docker_ssh_key_path and __import__("pathlib").Path(s.docker_ssh_key_path).is_file()
        ),
        "time": format_de(now_berlin()),
    }


@router.post("/setup")
async def apply_setup(payload: SetupPayload) -> dict[str, Any]:
    """Update in-memory settings for the running process.

    For durable config, set the matching environment variables / `.env` and restart.
    Empty secret/password fields keep the previously configured values.
    """
    s = get_settings()
    raw = payload.model_dump()
    # Do not wipe secrets when the setup form leaves password fields blank
    if not raw.get("proxmox_token_secret"):
        raw.pop("proxmox_token_secret", None)
    if not raw.get("proxmox_password"):
        raw.pop("proxmox_password", None)
    for key, value in raw.items():
        if hasattr(s, key):
            object.__setattr__(s, key, value)
    return {
        "ok": True,
        "message": (
            "Laufzeit-Konfiguration aktualisiert. "
            "Für Persistenz bitte Umgebungsvariablen setzen und Container neu starten."
        ),
        "proxmox_configured": s.proxmox_configured,
        "time": format_de(now_berlin()),
    }
