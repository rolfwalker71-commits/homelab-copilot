"""API routes: health, topology, discovery control, setup, Docker control."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.core import docker_control as docker_ctl
from app.core.locale import format_de, now_berlin
from app.core.registry import registry


def _ssh_key_present(s: Settings) -> bool:
    return docker_ctl.ssh_key_present(s)


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


class DockerActionPayload(BaseModel):
    parent_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)


class DockerComposeRestartPayload(BaseModel):
    parent_id: str = Field(..., min_length=1)
    project: str = Field(..., min_length=1)


class DockerComposeFileSavePayload(BaseModel):
    parent_id: str = Field(..., min_length=1)
    project: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    content: str = Field(..., max_length=524288)


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
        "docker_ssh_key_present": _ssh_key_present(s),
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


def _docker_http_error(exc: docker_ctl.DockerControlError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get("/guests/{guest_id}/rrd")
async def guest_rrd(
    guest_id: str,
    request: Request,
    timeframe: str = Query("hour", pattern="^(hour|day|week|month|year)$"),
) -> dict[str, Any]:
    """Proxmox RRD samples for guest header sparklines (CPU + Net)."""
    engine = request.app.state.discovery_engine
    try:
        return await engine.fetch_guest_rrd(guest_id, timeframe=timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Proxmox-RRD fehlgeschlagen: {exc}",
        ) from exc


@router.get("/guests/{guest_id}/storage")
async def guest_storage(guest_id: str, request: Request) -> dict[str, Any]:
    """Live disk usage and volume/mount assignment for an LXC or QEMU guest."""
    engine = request.app.state.discovery_engine
    try:
        return await engine.fetch_guest_storage(guest_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Proxmox-Guest-Storage fehlgeschlagen: {exc}",
        ) from exc


@router.post("/guests/{guest_id}/power/{action}")
async def guest_power(
    guest_id: str,
    action: Literal["start", "stop", "shutdown", "reboot"],
    request: Request,
) -> dict[str, Any]:
    """Start / Stop / Shutdown / Reboot an LXC or QEMU guest via Proxmox."""
    engine = request.app.state.discovery_engine
    try:
        return await engine.guest_power(guest_id, action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Proxmox-Power-Aktion fehlgeschlagen: {exc}",
        ) from exc


def _proxmox_node_http_error(exc: Exception, *, label: str) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, RuntimeError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=502, detail=f"{label}: {exc}")


@router.get("/nodes/{node}/status")
async def node_status(node: str, request: Request) -> dict[str, Any]:
    """Live Proxmox node status (loadavg, rootfs, versions, memory, CPU)."""
    engine = request.app.state.discovery_engine
    try:
        return await engine.fetch_node_status(node)
    except Exception as exc:
        raise _proxmox_node_http_error(exc, label="Proxmox-Node-Status fehlgeschlagen") from exc


@router.get("/nodes/{node}/rrd")
async def node_rrd(
    node: str,
    request: Request,
    timeframe: str = Query("hour", pattern="^(hour|day|week|month|year)$"),
) -> dict[str, Any]:
    """Proxmox RRD samples for node charts (CPU, RAM, Netz)."""
    engine = request.app.state.discovery_engine
    try:
        return await engine.fetch_node_rrd(node, timeframe=timeframe)
    except Exception as exc:
        raise _proxmox_node_http_error(exc, label="Proxmox-Node-RRD fehlgeschlagen") from exc


@router.get("/nodes/{node}/storage")
async def node_storage(node: str, request: Request) -> dict[str, Any]:
    """Storage pools on a Proxmox node (used / total / avail)."""
    engine = request.app.state.discovery_engine
    try:
        return await engine.fetch_node_storage(node)
    except Exception as exc:
        raise _proxmox_node_http_error(exc, label="Proxmox-Storage fehlgeschlagen") from exc


@router.post("/docker/compose/restart")
async def docker_compose_restart(
    payload: DockerComposeRestartPayload,
    request: Request,
) -> dict[str, Any]:
    """Restart all services in a Compose project on the parent guest."""
    settings = get_settings()
    store = request.app.state.topology_store
    try:
        return await docker_ctl.run_compose_restart(
            settings,
            parent_id=payload.parent_id,
            project=payload.project,
            snapshot=store.snapshot,
        )
    except docker_ctl.DockerControlError as exc:
        raise _docker_http_error(exc) from exc


@router.get("/docker/compose/file")
async def docker_compose_file_get(
    request: Request,
    parent_id: str = Query(..., min_length=1),
    project: str = Query(..., min_length=1),
    path: str | None = Query(None),
) -> dict[str, Any]:
    """Read docker-compose.yml (or sibling compose file) for a stack."""
    settings = get_settings()
    store = request.app.state.topology_store
    try:
        return await docker_ctl.fetch_compose_file(
            settings,
            parent_id=parent_id,
            project=project,
            snapshot=store.snapshot,
            path=path,
        )
    except docker_ctl.DockerControlError as exc:
        raise _docker_http_error(exc) from exc


@router.put("/docker/compose/file")
async def docker_compose_file_put(
    payload: DockerComposeFileSavePayload,
    request: Request,
) -> dict[str, Any]:
    """Write compose file back to the guest (creates .bak first)."""
    settings = get_settings()
    store = request.app.state.topology_store
    try:
        return await docker_ctl.save_compose_file(
            settings,
            parent_id=payload.parent_id,
            project=payload.project,
            path=payload.path,
            content=payload.content,
            snapshot=store.snapshot,
        )
    except docker_ctl.DockerControlError as exc:
        raise _docker_http_error(exc) from exc


@router.post("/docker/{action}")
async def docker_container_action(
    action: Literal["start", "stop", "restart"],
    payload: DockerActionPayload,
    request: Request,
) -> dict[str, Any]:
    """Start / Stop / Restart a container via SSH (or local Docker socket)."""
    settings = get_settings()
    store = request.app.state.topology_store
    try:
        return await docker_ctl.run_container_action(
            settings,
            parent_id=payload.parent_id,
            name=payload.name,
            action=action,
            snapshot=store.snapshot,
        )
    except docker_ctl.DockerControlError as exc:
        raise _docker_http_error(exc) from exc


@router.get("/docker/logs")
async def docker_logs(
    request: Request,
    parent_id: str = Query(..., min_length=1),
    name: str = Query(..., min_length=1),
    tail: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    """Return last N lines of ``docker logs`` for a container."""
    settings = get_settings()
    store = request.app.state.topology_store
    try:
        return await docker_ctl.fetch_logs(
            settings,
            parent_id=parent_id,
            name=name,
            snapshot=store.snapshot,
            tail=tail,
        )
    except docker_ctl.DockerControlError as exc:
        raise _docker_http_error(exc) from exc
