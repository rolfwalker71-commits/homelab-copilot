"""Docker start/stop/restart/logs via SSH (or local socket)."""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from pathlib import Path
from typing import Any, Literal

import asyncssh

from app.config import Settings
from app.core.models import TopologyEntity, TopologySnapshot

logger = logging.getLogger(__name__)

DockerAction = Literal["start", "stop", "restart"]

# Docker container / compose project name allowlist (no shell metacharacters).
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


class DockerControlError(Exception):
    """User-facing German error for Docker control failures."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def validate_docker_name(name: str, *, kind: str = "Container") -> str:
    name = (name or "").strip()
    if not name or not _SAFE_NAME.match(name):
        raise DockerControlError(
            f"Ungültiger {kind}-Name. Erlaubt: Buchstaben, Zahlen, _ . - "
            f"(muss mit Buchstabe/Zahl beginnen)."
        )
    return name


def ssh_key_path(settings: Settings) -> Path:
    configured = Path(settings.docker_ssh_key_path)
    if configured.is_file():
        return configured
    fallback = Path(settings.data_dir) / "ssh" / "id_ed25519"
    if fallback.is_file():
        return fallback
    return configured


def ssh_key_present(settings: Settings) -> bool:
    return ssh_key_path(settings).is_file()


def resolve_parent_ip(snapshot: TopologySnapshot | None, parent_id: str) -> str | None:
    """Return SSH target IP for a guest parent_id, or None for local docker."""
    if parent_id == "local:docker":
        return None
    if snapshot is None:
        raise DockerControlError(
            "Keine Topologie geladen — bitte zuerst Discovery ausführen.",
            status_code=404,
        )
    for g in snapshot.guests:
        if g.id == parent_id:
            if not g.ip_addresses:
                raise DockerControlError(
                    f"Guest „{g.name}“ hat keine IP — SSH nicht möglich.",
                    status_code=400,
                )
            return g.ip_addresses[0]
    # Orphan / unknown parent: try container hostname/IP from snapshot
    for c in snapshot.containers:
        if c.parent_id == parent_id and c.ip_addresses:
            return c.ip_addresses[0]
        if c.parent_id == parent_id and c.hostname and _looks_like_ip(c.hostname):
            return c.hostname
    raise DockerControlError(
        f"Parent „{parent_id}“ nicht in der Topologie gefunden.",
        status_code=404,
    )


def _looks_like_ip(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def find_container(
    snapshot: TopologySnapshot | None, parent_id: str, name: str
) -> TopologyEntity | None:
    if snapshot is None:
        return None
    for c in snapshot.containers:
        if c.parent_id == parent_id and c.name == name:
            return c
    return None


async def run_container_action(
    settings: Settings,
    *,
    parent_id: str,
    name: str,
    action: DockerAction,
    snapshot: TopologySnapshot | None,
) -> dict[str, Any]:
    name = validate_docker_name(name)
    if action not in ("start", "stop", "restart"):
        raise DockerControlError(f"Unbekannte Aktion: {action}")

    ip = resolve_parent_ip(snapshot, parent_id)
    if ip is None:
        return await _local_container_action(settings, name=name, action=action)

    if not ssh_key_present(settings):
        raise DockerControlError(
            "SSH-Schlüssel fehlt — Docker-Steuerung auf Remotes nicht möglich. "
            f"Erwartet unter {ssh_key_path(settings)}.",
            status_code=503,
        )

    cmd = f"docker {action} -- {shlex.quote(name)}"
    stdout, stderr, code = await _ssh_run(settings, ip, cmd)
    if code != 0:
        detail = (stderr or stdout or "").strip() or f"exit {code}"
        raise DockerControlError(
            f"docker {action} fehlgeschlagen auf {ip}: {detail}",
            status_code=502,
        )
    return {
        "ok": True,
        "action": action,
        "name": name,
        "parent_id": parent_id,
        "via": "ssh",
        "host": ip,
        "message": f"Container „{name}“: {action} OK.",
    }


async def run_compose_restart(
    settings: Settings,
    *,
    parent_id: str,
    project: str,
    snapshot: TopologySnapshot | None,
) -> dict[str, Any]:
    project = validate_docker_name(project, kind="Compose-Projekt")
    ip = resolve_parent_ip(snapshot, parent_id)
    if ip is None:
        return await _local_compose_restart(settings, project=project)

    if not ssh_key_present(settings):
        raise DockerControlError(
            "SSH-Schlüssel fehlt — Compose-Steuerung nicht möglich.",
            status_code=503,
        )

    # Prefer compose v2; fall back to restarting labeled containers.
    cmd = f"docker compose -p {shlex.quote(project)} restart"
    stdout, stderr, code = await _ssh_run(settings, ip, cmd)
    if code != 0:
        # Fallback: restart each matching container from topology
        names = [
            c.name
            for c in (snapshot.containers if snapshot else [])
            if c.parent_id == parent_id
            and (c.labels or {}).get("com.docker.compose.project") == project
            and _SAFE_NAME.match(c.name)
        ]
        if not names:
            detail = (stderr or stdout or "").strip() or f"exit {code}"
            raise DockerControlError(
                f"Compose-Restart für „{project}“ fehlgeschlagen: {detail}",
                status_code=502,
            )
        quoted = " ".join(shlex.quote(n) for n in names)
        stdout, stderr, code = await _ssh_run(settings, ip, f"docker restart -- {quoted}")
        if code != 0:
            detail = (stderr or stdout or "").strip() or f"exit {code}"
            raise DockerControlError(
                f"Stack-Restart „{project}“ fehlgeschlagen: {detail}",
                status_code=502,
            )
        return {
            "ok": True,
            "action": "compose_restart",
            "project": project,
            "parent_id": parent_id,
            "via": "ssh",
            "host": ip,
            "containers": names,
            "message": f"Stack „{project}“ neu gestartet ({len(names)} Container).",
        }

    return {
        "ok": True,
        "action": "compose_restart",
        "project": project,
        "parent_id": parent_id,
        "via": "ssh",
        "host": ip,
        "message": f"Stack „{project}“ neu gestartet.",
    }


async def fetch_logs(
    settings: Settings,
    *,
    parent_id: str,
    name: str,
    snapshot: TopologySnapshot | None,
    tail: int = 200,
) -> dict[str, Any]:
    name = validate_docker_name(name)
    tail = max(1, min(int(tail), 2000))
    ip = resolve_parent_ip(snapshot, parent_id)
    if ip is None:
        text = await _local_logs(settings, name=name, tail=tail)
        return {
            "ok": True,
            "name": name,
            "parent_id": parent_id,
            "via": "local",
            "tail": tail,
            "logs": text,
        }

    if not ssh_key_present(settings):
        raise DockerControlError(
            "SSH-Schlüssel fehlt — Logs nicht abrufbar.",
            status_code=503,
        )

    cmd = f"docker logs --tail {tail} -- {shlex.quote(name)} 2>&1"
    stdout, stderr, code = await _ssh_run(settings, ip, cmd)
    # docker logs writes to stderr for container stderr; we merged via 2>&1 in shell
    text = (stdout or stderr or "").rstrip()
    if code != 0 and not text:
        raise DockerControlError(
            f"Logs für „{name}“ fehlgeschlagen (exit {code}).",
            status_code=502,
        )
    return {
        "ok": True,
        "name": name,
        "parent_id": parent_id,
        "via": "ssh",
        "host": ip,
        "tail": tail,
        "logs": text,
    }


async def _ssh_run(settings: Settings, ip: str, cmd: str) -> tuple[str, str, int]:
    key = ssh_key_path(settings)
    connect_timeout = min(3.0, settings.docker_ssh_timeout + 1.0)
    cmd_timeout = max(5.0, settings.docker_ssh_timeout + 10.0)
    try:
        async with asyncssh.connect(
            ip,
            port=settings.docker_ssh_port,
            username=settings.docker_ssh_user,
            client_keys=[str(key)],
            known_hosts=None,
            connect_timeout=connect_timeout,
            login_timeout=connect_timeout,
        ) as conn:
            result = await conn.run(cmd, check=False, timeout=cmd_timeout)
    except asyncio.TimeoutError as exc:
        raise DockerControlError(
            f"SSH-Timeout zu {ip} — Host nicht erreichbar?",
            status_code=504,
        ) from exc
    except (OSError, asyncssh.Error) as exc:
        logger.warning("SSH Docker control %s: %s", ip, exc)
        raise DockerControlError(
            f"SSH zu {ip} fehlgeschlagen: {exc}",
            status_code=502,
        ) from exc

    stdout = result.stdout if isinstance(result.stdout, str) else (result.stdout or b"").decode(
        "utf-8", errors="replace"
    )
    stderr = result.stderr if isinstance(result.stderr, str) else (result.stderr or b"").decode(
        "utf-8", errors="replace"
    )
    return stdout, stderr, int(result.exit_status or 0)


async def _local_container_action(
    settings: Settings, *, name: str, action: DockerAction
) -> dict[str, Any]:
    def _sync() -> dict[str, Any]:
        import docker

        client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")
        try:
            c = client.containers.get(name)
            getattr(c, action)()
        finally:
            client.close()
        return {
            "ok": True,
            "action": action,
            "name": name,
            "parent_id": "local:docker",
            "via": "local",
            "message": f"Container „{name}“: {action} OK.",
        }

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        raise DockerControlError(
            f"Lokale Docker-Aktion fehlgeschlagen: {exc}",
            status_code=502,
        ) from exc


async def _local_compose_restart(settings: Settings, *, project: str) -> dict[str, Any]:
    def _sync() -> dict[str, Any]:
        import docker

        client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")
        try:
            matched = client.containers.list(
                all=True,
                filters={"label": f"com.docker.compose.project={project}"},
            )
            if not matched:
                raise RuntimeError(f"Keine Container für Projekt „{project}“.")
            names = []
            for c in matched:
                c.restart()
                names.append(c.name.lstrip("/") if c.name else c.short_id)
        finally:
            client.close()
        return {
            "ok": True,
            "action": "compose_restart",
            "project": project,
            "parent_id": "local:docker",
            "via": "local",
            "containers": names,
            "message": f"Stack „{project}“ neu gestartet ({len(names)} Container).",
        }

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        raise DockerControlError(
            f"Lokaler Compose-Restart fehlgeschlagen: {exc}",
            status_code=502,
        ) from exc


async def _local_logs(settings: Settings, *, name: str, tail: int) -> str:
    def _sync() -> str:
        import docker

        client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")
        try:
            c = client.containers.get(name)
            raw = c.logs(tail=tail, timestamps=False)
        finally:
            client.close()
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace").rstrip()
        return str(raw).rstrip()

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        raise DockerControlError(
            f"Lokale Logs fehlgeschlagen: {exc}",
            status_code=502,
        ) from exc
