"""Docker start/stop/restart/logs via SSH (or local socket)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

import asyncssh

from app.config import Settings
from app.core.models import TopologyEntity, TopologySnapshot
from app.core.ssh_endpoint import find_topology_entity, resolve_ssh_endpoint

logger = logging.getLogger(__name__)

DockerAction = Literal["start", "stop", "restart"]

# Docker container / compose project name allowlist (no shell metacharacters).
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_COMPOSE_BASENAMES = frozenset(
    {
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    }
)
_MAX_COMPOSE_BYTES = 512 * 1024


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
    """Return SSH target IP for a guest/host parent_id, or None for local docker."""
    if parent_id == "local:docker":
        return None
    if snapshot is None:
        raise DockerControlError(
            "Keine Topologie geladen — bitte zuerst Discovery ausführen.",
            status_code=404,
        )
    entity = find_topology_entity(snapshot, parent_id)
    if entity is not None:
        if not entity.ip_addresses:
            raise DockerControlError(
                f"„{entity.name}“ hat keine IP — SSH nicht möglich.",
                status_code=400,
            )
        return entity.ip_addresses[0]
    for c in snapshot.containers:
        if c.parent_id == parent_id and c.ip_addresses:
            return c.ip_addresses[0]
        if c.parent_id == parent_id and c.hostname and _looks_like_ip(c.hostname):
            return c.hostname
    raise DockerControlError(
        f"Parent „{parent_id}“ nicht in der Topologie gefunden.",
        status_code=404,
    )


def resolve_parent_ssh(
    settings: Settings,
    snapshot: TopologySnapshot | None,
    parent_id: str,
) -> tuple[str | None, int, str]:
    """Return (ip or None for local, port, username)."""
    if parent_id == "local:docker":
        return None, settings.docker_ssh_port, settings.docker_ssh_user
    if snapshot is None:
        raise DockerControlError(
            "Keine Topologie geladen — bitte zuerst Discovery ausführen.",
            status_code=404,
        )
    ep = resolve_ssh_endpoint(snapshot, parent_id, settings)
    if ep is not None:
        return ep.ip, ep.port, ep.username
    entity = find_topology_entity(snapshot, parent_id)
    if entity is not None:
        raise DockerControlError(
            f"„{entity.name}“ hat keine IP — SSH nicht möglich.",
            status_code=400,
        )
    for c in snapshot.containers:
        if c.parent_id == parent_id and c.ip_addresses:
            return c.ip_addresses[0], settings.docker_ssh_port, settings.docker_ssh_user
        if c.parent_id == parent_id and c.hostname and _looks_like_ip(c.hostname):
            return c.hostname, settings.docker_ssh_port, settings.docker_ssh_user
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

    ip, port, user = resolve_parent_ssh(settings, snapshot, parent_id)
    if ip is None:
        return await _local_container_action(settings, name=name, action=action)

    if not ssh_key_present(settings):
        raise DockerControlError(
            "SSH-Schlüssel fehlt — Docker-Steuerung auf Remotes nicht möglich. "
            f"Erwartet unter {ssh_key_path(settings)}.",
            status_code=503,
        )

    cmd = f"docker {action} -- {shlex.quote(name)}"
    stdout, stderr, code = await _ssh_run(
        settings, ip, cmd, port=port, username=user
    )
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
    ip, port, user = resolve_parent_ssh(settings, snapshot, parent_id)
    if ip is None:
        return await _local_compose_restart(settings, project=project)

    if not ssh_key_present(settings):
        raise DockerControlError(
            "SSH-Schlüssel fehlt — Compose-Steuerung nicht möglich.",
            status_code=503,
        )

    # Prefer compose v2; fall back to restarting labeled containers.
    cmd = f"docker compose -p {shlex.quote(project)} restart"
    stdout, stderr, code = await _ssh_run(
        settings, ip, cmd, port=port, username=user
    )
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
        stdout, stderr, code = await _ssh_run(
            settings, ip, f"docker restart -- {quoted}", port=port, username=user
        )
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
    ip, port, user = resolve_parent_ssh(settings, snapshot, parent_id)
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
    stdout, stderr, code = await _ssh_run(
        settings, ip, cmd, port=port, username=user
    )
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


async def _ssh_run(
    settings: Settings,
    ip: str,
    cmd: str,
    *,
    port: int | None = None,
    username: str | None = None,
    cmd_timeout: float | None = None,
) -> tuple[str, str, int]:
    key = ssh_key_path(settings)
    connect_timeout = min(3.0, settings.docker_ssh_timeout + 1.0)
    timeout = (
        float(cmd_timeout)
        if cmd_timeout is not None
        else max(5.0, settings.docker_ssh_timeout + 10.0)
    )
    try:
        async with asyncssh.connect(
            ip,
            port=int(port or settings.docker_ssh_port),
            username=(username or settings.docker_ssh_user),
            client_keys=[str(key)],
            known_hosts=None,
            connect_timeout=connect_timeout,
            login_timeout=connect_timeout,
        ) as conn:
            result = await conn.run(cmd, check=False, timeout=timeout)
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


async def _ssh_run_stream(
    settings: Settings,
    ip: str,
    cmd: str,
    *,
    port: int | None = None,
    username: str | None = None,
    cmd_timeout: float = 1800.0,
    on_line: Callable[[str], Awaitable[None] | None] | None = None,
) -> tuple[str, str, int]:
    """Stream stdout/stderr lines (docker pull). Longer timeout than discovery SSH."""
    key = ssh_key_path(settings)
    connect_timeout = min(8.0, max(3.0, settings.docker_ssh_timeout + 4.0))

    async def _emit(text: str) -> None:
        if not on_line:
            return
        result = on_line(text)
        if asyncio.iscoroutine(result):
            await result

    try:
        async with asyncssh.connect(
            ip,
            port=int(port or settings.docker_ssh_port),
            username=(username or settings.docker_ssh_user),
            client_keys=[str(key)],
            known_hosts=None,
            connect_timeout=connect_timeout,
            login_timeout=connect_timeout,
        ) as conn:
            try:
                process = await conn.create_process(
                    cmd,
                    stderr=asyncssh.STDOUT,
                    encoding="utf-8",
                    errors="replace",
                )
            except TypeError:
                process = await conn.create_process(cmd, stderr=asyncssh.STDOUT)
            chunks: list[str] = []

            async def _read() -> None:
                reader = process.stdout
                while True:
                    raw = await reader.readline()
                    if not raw:
                        break
                    line = (
                        raw.decode("utf-8", errors="replace")
                        if isinstance(raw, bytes)
                        else raw
                    )
                    chunks.append(line)
                    text = line.rstrip("\r\n")
                    if text:
                        await _emit(text)
                await process.wait()

            try:
                await asyncio.wait_for(_read(), timeout=cmd_timeout)
            except asyncio.TimeoutError as exc:
                try:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except Exception:
                    pass
                raise DockerControlError(
                    f"SSH-Befehl-Timeout zu {ip} ({cmd_timeout:.0f}s) — "
                    "docker pull/restart hängt?",
                    status_code=504,
                ) from exc
            return "".join(chunks), "", int(process.exit_status or 0)
    except DockerControlError:
        raise
    except asyncio.TimeoutError as exc:
        raise DockerControlError(
            f"SSH-Timeout zu {ip} — Host nicht erreichbar?",
            status_code=504,
        ) from exc
    except (OSError, asyncssh.Error) as exc:
        logger.warning("SSH Docker stream %s: %s", ip, exc)
        raise DockerControlError(
            f"SSH zu {ip} fehlgeschlagen: {exc}",
            status_code=502,
        ) from exc


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


def _normalize_abs_path(path: str) -> str:
    raw = (path or "").strip()
    if not raw or "\x00" in raw:
        raise DockerControlError("Ungültiger Dateipfad.", status_code=400)
    if not raw.startswith("/"):
        raise DockerControlError(
            "Compose-Pfad muss absolut sein.",
            status_code=400,
        )
    # Collapse // and reject .. segments without resolving host FS.
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise DockerControlError(
                "Pfad darf keine „..“-Segmente enthalten.",
                status_code=400,
            )
        parts.append(part)
    return "/" + "/".join(parts)


def _compose_meta_from_inspects(inspects: list[dict[str, Any]]) -> dict[str, Any]:
    working_dir: str | None = None
    config_files: list[str] = []
    for info in inspects:
        labels = (info.get("Config") or {}).get("Labels") or {}
        wd = labels.get("com.docker.compose.project.working_dir")
        if wd and not working_dir:
            working_dir = wd.rstrip("/")
        cf = labels.get("com.docker.compose.project.config_files")
        if cf:
            for part in re.split(r"[,:]", cf):
                part = part.strip()
                if not part:
                    continue
                try:
                    norm = _normalize_abs_path(part)
                except DockerControlError:
                    continue
                if norm not in config_files:
                    config_files.append(norm)
    candidates = list(config_files)
    if working_dir:
        try:
            wd_norm = _normalize_abs_path(working_dir)
        except DockerControlError:
            wd_norm = None
        if wd_norm:
            working_dir = wd_norm
            for name in _COMPOSE_BASENAMES:
                p = f"{wd_norm}/{name}"
                if p not in candidates:
                    candidates.append(p)
    return {
        "working_dir": working_dir,
        "config_files": config_files,
        "candidates": candidates,
    }


def _path_allowed(path: str, *, working_dir: str | None, config_files: list[str]) -> bool:
    if path in config_files:
        return True
    if working_dir and path.startswith(working_dir.rstrip("/") + "/"):
        return Path(path).name in _COMPOSE_BASENAMES
    return False


async def _inspect_compose_project(
    settings: Settings,
    *,
    parent_id: str,
    project: str,
    snapshot: TopologySnapshot | None,
) -> tuple[str | None, list[dict[str, Any]]]:
    project = validate_docker_name(project, kind="Compose-Projekt")
    ip, port, user = resolve_parent_ssh(settings, snapshot, parent_id)
    if ip is None:
        inspects = await _local_inspect_project(settings, project)
        return None, inspects
    if not ssh_key_present(settings):
        raise DockerControlError(
            "SSH-Schlüssel fehlt — Compose-Datei nicht erreichbar.",
            status_code=503,
        )
    inspects = await _remote_inspect_project(
        settings, ip, project, port=port, username=user
    )
    return ip, inspects


async def _local_inspect_project(settings: Settings, project: str) -> list[dict[str, Any]]:
    def _sync() -> list[dict[str, Any]]:
        import docker

        client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")
        try:
            matched = client.containers.list(
                all=True,
                filters={"label": f"com.docker.compose.project={project}"},
            )
            return [c.attrs for c in matched]
        finally:
            client.close()

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        raise DockerControlError(
            f"Lokales docker inspect fehlgeschlagen: {exc}",
            status_code=502,
        ) from exc


async def _remote_inspect_project(
    settings: Settings,
    ip: str,
    project: str,
    *,
    port: int | None = None,
    username: str | None = None,
) -> list[dict[str, Any]]:
    list_cmd = (
        "docker ps -aq --filter "
        f"label=com.docker.compose.project={shlex.quote(project)}"
    )
    stdout, _, code = await _ssh_run(
        settings, ip, list_cmd, port=port, username=username
    )
    if code != 0:
        return []
    ids = [x.strip() for x in stdout.splitlines() if x.strip()][:40]
    if not ids:
        return []
    id_args = " ".join(shlex.quote(i) for i in ids)
    stdout, stderr, code = await _ssh_run(
        settings, ip, f"docker inspect {id_args}", port=port, username=username
    )
    if code != 0:
        detail = (stderr or stdout or "").strip() or f"exit {code}"
        raise DockerControlError(
            f"docker inspect fehlgeschlagen: {detail}",
            status_code=502,
        )
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DockerControlError(
            f"docker inspect JSON ungültig: {exc}",
            status_code=502,
        ) from exc
    return data if isinstance(data, list) else []


async def _resolve_existing_compose_paths(
    settings: Settings,
    *,
    ip: str | None,
    candidates: list[str],
    config_files: list[str],
) -> list[str]:
    existing: list[str] = []
    # Prefer label-declared files first.
    ordered = [p for p in candidates if p in config_files] + [
        p for p in candidates if p not in config_files
    ]
    for path in ordered:
        if await _remote_or_local_readable(settings, ip, path):
            if path not in existing:
                existing.append(path)
    return existing


async def _remote_or_local_readable(
    settings: Settings, ip: str | None, path: str
) -> bool:
    check = f"test -r {shlex.quote(path)} && test -f {shlex.quote(path)}"
    if ip is None:
        proc = await asyncio.create_subprocess_shell(
            check,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        code = await proc.wait()
        return code == 0
    _, _, code = await _ssh_run(settings, ip, check)
    return code == 0


async def fetch_compose_file(
    settings: Settings,
    *,
    parent_id: str,
    project: str,
    snapshot: TopologySnapshot | None,
    path: str | None = None,
) -> dict[str, Any]:
    ip, inspects = await _inspect_compose_project(
        settings, parent_id=parent_id, project=project, snapshot=snapshot
    )
    if not inspects:
        raise DockerControlError(
            f"Keine Container für Compose-Projekt „{project}“ gefunden.",
            status_code=404,
        )
    meta = _compose_meta_from_inspects(inspects)
    existing = await _resolve_existing_compose_paths(
        settings,
        ip=ip,
        candidates=meta["candidates"],
        config_files=meta["config_files"],
    )
    if not existing:
        raise DockerControlError(
            "Keine Compose-Datei gefunden (Labels/Working-Dir). "
            "Oft fehlt com.docker.compose.project.working_dir.",
            status_code=404,
        )

    target = _normalize_abs_path(path) if path else existing[0]
    if target not in existing and not _path_allowed(
        target, working_dir=meta["working_dir"], config_files=meta["config_files"]
    ):
        raise DockerControlError(
            "Pfad liegt außerhalb der erlaubten Compose-Dateien dieses Stacks.",
            status_code=403,
        )
    if target not in existing:
        raise DockerControlError(
            f"Datei nicht gefunden: {target}",
            status_code=404,
        )

    _ip, port, user = (
        resolve_parent_ssh(settings, snapshot, parent_id)
        if ip
        else (None, settings.docker_ssh_port, settings.docker_ssh_user)
    )
    content = await _read_text_file(
        settings, ip, target, port=port, username=user
    )
    return {
        "ok": True,
        "parent_id": parent_id,
        "project": project,
        "path": target,
        "files": existing,
        "working_dir": meta["working_dir"],
        "content": content,
        "editable": True,
        "via": "local" if ip is None else "ssh",
        "host": ip,
        "message": f"Compose-Datei {Path(target).name} geladen.",
    }


async def save_compose_file(
    settings: Settings,
    *,
    parent_id: str,
    project: str,
    path: str,
    content: str,
    snapshot: TopologySnapshot | None,
) -> dict[str, Any]:
    if content is None:
        raise DockerControlError("Inhalt fehlt.", status_code=400)
    raw = content.encode("utf-8")
    if len(raw) > _MAX_COMPOSE_BYTES:
        raise DockerControlError(
            f"Datei zu groß (max {_MAX_COMPOSE_BYTES // 1024} KiB).",
            status_code=413,
        )
    target = _normalize_abs_path(path)
    ip, inspects = await _inspect_compose_project(
        settings, parent_id=parent_id, project=project, snapshot=snapshot
    )
    if not inspects:
        raise DockerControlError(
            f"Keine Container für Compose-Projekt „{project}“ gefunden.",
            status_code=404,
        )
    meta = _compose_meta_from_inspects(inspects)
    if not _path_allowed(
        target, working_dir=meta["working_dir"], config_files=meta["config_files"]
    ):
        raise DockerControlError(
            "Schreiben nur in deklarierte Compose-Dateien / Working-Dir erlaubt.",
            status_code=403,
        )
    _ip, port, user = (
        resolve_parent_ssh(settings, snapshot, parent_id)
        if ip
        else (None, settings.docker_ssh_port, settings.docker_ssh_user)
    )
    await _write_text_file(
        settings, ip, target, content, backup=True, port=port, username=user
    )
    return {
        "ok": True,
        "parent_id": parent_id,
        "project": project,
        "path": target,
        "bytes": len(raw),
        "via": "local" if ip is None else "ssh",
        "host": ip,
        "message": (
            f"Gespeichert: {target} "
            f"(Backup als {target}.bak). Stack ggf. neu starten."
        ),
    }


async def _read_text_file(
    settings: Settings,
    ip: str | None,
    path: str,
    *,
    port: int | None = None,
    username: str | None = None,
) -> str:
    if ip is None:

        def _sync() -> str:
            p = Path(path)
            data = p.read_bytes()
            if len(data) > _MAX_COMPOSE_BYTES:
                raise DockerControlError(
                    f"Datei zu groß (max {_MAX_COMPOSE_BYTES // 1024} KiB).",
                    status_code=413,
                )
            return data.decode("utf-8", errors="replace")

        try:
            return await asyncio.to_thread(_sync)
        except DockerControlError:
            raise
        except OSError as exc:
            raise DockerControlError(
                f"Lesen fehlgeschlagen: {exc}",
                status_code=502,
            ) from exc

    # Prefer SFTP; fall back to base64 cat.
    key = ssh_key_path(settings)
    try:
        async with asyncssh.connect(
            ip,
            port=int(port or settings.docker_ssh_port),
            username=(username or settings.docker_ssh_user),
            client_keys=[str(key)],
            known_hosts=None,
            connect_timeout=min(3.0, settings.docker_ssh_timeout + 1.0),
            login_timeout=min(3.0, settings.docker_ssh_timeout + 1.0),
        ) as conn:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(path, "rb") as f:
                    data = await f.read(_MAX_COMPOSE_BYTES + 1)
        if len(data) > _MAX_COMPOSE_BYTES:
            raise DockerControlError(
                f"Datei zu groß (max {_MAX_COMPOSE_BYTES // 1024} KiB).",
                status_code=413,
            )
        if isinstance(data, str):
            return data
        return data.decode("utf-8", errors="replace")
    except DockerControlError:
        raise
    except (OSError, asyncssh.Error) as exc:
        logger.info("SFTP read failed for %s:%s (%s) — trying cat", ip, path, exc)
        cmd = f"base64 -w0 -- {shlex.quote(path)} 2>/dev/null || base64 -- {shlex.quote(path)}"
        stdout, stderr, code = await _ssh_run(
            settings, ip, cmd, port=port, username=username
        )
        if code != 0:
            detail = (stderr or stdout or "").strip() or f"exit {code}"
            raise DockerControlError(
                f"Lesen von {path} fehlgeschlagen: {detail}",
                status_code=502,
            ) from exc
        try:
            raw = base64.b64decode(stdout.strip(), validate=False)
        except Exception as b64exc:
            raise DockerControlError(
                f"Datei-Inhalt ungültig (base64): {b64exc}",
                status_code=502,
            ) from b64exc
        if len(raw) > _MAX_COMPOSE_BYTES:
            raise DockerControlError(
                f"Datei zu groß (max {_MAX_COMPOSE_BYTES // 1024} KiB).",
                status_code=413,
            )
        return raw.decode("utf-8", errors="replace")


async def _write_text_file(
    settings: Settings,
    ip: str | None,
    path: str,
    content: str,
    *,
    backup: bool = True,
    port: int | None = None,
    username: str | None = None,
) -> None:
    data = content.encode("utf-8")
    if ip is None:

        def _sync() -> None:
            p = Path(path)
            if backup and p.is_file():
                bak = Path(str(p) + ".bak")
                bak.write_bytes(p.read_bytes())
            p.write_bytes(data)

        try:
            await asyncio.to_thread(_sync)
            return
        except OSError as exc:
            raise DockerControlError(
                f"Schreiben fehlgeschlagen: {exc}",
                status_code=502,
            ) from exc

    key = ssh_key_path(settings)
    try:
        async with asyncssh.connect(
            ip,
            port=int(port or settings.docker_ssh_port),
            username=(username or settings.docker_ssh_user),
            client_keys=[str(key)],
            known_hosts=None,
            connect_timeout=min(3.0, settings.docker_ssh_timeout + 1.0),
            login_timeout=min(3.0, settings.docker_ssh_timeout + 1.0),
        ) as conn:
            if backup:
                await conn.run(
                    f"if [ -f {shlex.quote(path)} ]; then "
                    f"cp -a -- {shlex.quote(path)} {shlex.quote(path + '.bak')}; fi",
                    check=False,
                )
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(path, "wb") as f:
                    await f.write(data)
        return
    except (OSError, asyncssh.Error) as exc:
        logger.info("SFTP write failed for %s:%s (%s) — trying pipe", ip, path, exc)
        sftp_err = exc

    b64 = base64.b64encode(data).decode("ascii")
    # Keep command size reasonable; for large files SFTP should have worked.
    if len(b64) > 700_000:
        raise DockerControlError(
            f"Schreiben per SSH fehlgeschlagen: {sftp_err}",
            status_code=502,
        )
    bak = (
        f"if [ -f {shlex.quote(path)} ]; then "
        f"cp -a -- {shlex.quote(path)} {shlex.quote(path + '.bak')}; fi && "
        if backup
        else ""
    )
    cmd = (
        f"{bak}printf '%s' {shlex.quote(b64)} | base64 -d > {shlex.quote(path)}.tmp "
        f"&& mv -f -- {shlex.quote(path)}.tmp {shlex.quote(path)}"
    )
    stdout, stderr, code = await _ssh_run(
        settings, ip, cmd, port=port, username=username
    )
    if code != 0:
        detail = (stderr or stdout or "").strip() or f"exit {code}"
        raise DockerControlError(
            f"Schreiben von {path} fehlgeschlagen: {detail}",
            status_code=502,
        )


_LOCAL_ONLY_STATUS = "Nur lokal — Remote-Vergleich nicht möglich."


def _unique_cmd_detail(text: str, *, fallback: str) -> str:
    """Collapse identical stderr lines (docker --format repeats per object)."""
    unique: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        unique.append(line)
    return "\n".join(unique) if unique else fallback


def _parse_inspect_payload(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw or "")
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _repo_digests_from_image(image: dict[str, Any]) -> list[str]:
    raw = image.get("RepoDigests")
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if x]


def _local_sha_from_digests(digests: list[str]) -> str:
    for digest in digests:
        if "@sha256:" in digest:
            return digest.split("@", 1)[-1]
    return ""


def _index_image_digests(dest: dict[str, list[str]], image: dict[str, Any]) -> None:
    digests = _repo_digests_from_image(image)
    keys: list[str] = []
    image_id = str(image.get("Id") or "").strip()
    if image_id:
        keys.append(image_id)
    tags = image.get("RepoTags")
    if isinstance(tags, list):
        keys.extend(str(tag) for tag in tags if tag)
    for key in keys:
        dest[key] = digests


async def _inspect_image_repo_digests(
    settings: Settings,
    ip: str,
    image_refs: list[str],
    *,
    port: int | None,
    username: str | None,
    cmd_timeout: float | None = None,
) -> dict[str, list[str]]:
    """Map image id/tag → RepoDigests. Missing key/empty list stays empty."""
    dest: dict[str, list[str]] = {}
    refs = list(dict.fromkeys(r for r in image_refs if r))
    if not refs:
        return dest

    async def _load(quoted: str) -> list[dict[str, Any]] | None:
        stdout, _stderr, code = await _ssh_run(
            settings,
            ip,
            "docker inspect --type image -- " + quoted,
            port=port,
            username=username,
            cmd_timeout=cmd_timeout,
        )
        if code != 0:
            return None
        return _parse_inspect_payload(stdout)

    batch = await _load(" ".join(shlex.quote(r) for r in refs))
    if batch is not None:
        for image in batch:
            _index_image_digests(dest, image)
        return dest

    for ref in refs:
        one = await _load(shlex.quote(ref))
        if not one:
            continue
        for image in one:
            _index_image_digests(dest, image)
    return dest


async def scan_image_updates(
    settings: Settings,
    *,
    parent_id: str,
    snapshot: TopologySnapshot | None,
    project: str | None = None,
    inspect_timeout: float = 90.0,
) -> dict[str, Any]:
    """Compare local RepoDigests vs remote manifests (no pull, no restart)."""
    ip, port, user = resolve_parent_ssh(settings, snapshot, parent_id)
    if ip is None:
        raise DockerControlError(
            "Lokale Image-Prüfung ist nicht vorgesehen — bitte per SSH-Host scannen.",
            status_code=400,
        )
    if not ssh_key_present(settings):
        raise DockerControlError(
            "SSH-Schlüssel fehlt — Image-Scan nicht möglich.",
            status_code=503,
        )

    containers = [
        c
        for c in (snapshot.containers if snapshot else [])
        if c.parent_id == parent_id
        and (not project or (c.labels or {}).get("com.docker.compose.project") == project)
    ]
    if not containers:
        return {
            "ok": True,
            "parent_id": parent_id,
            "project": project,
            "updates": [],
            "count": 0,
            "message": "Keine Container auf diesem Host.",
        }

    names = [c.name for c in containers if _SAFE_NAME.match(c.name)][:80]
    if not names:
        return {
            "ok": True,
            "parent_id": parent_id,
            "updates": [],
            "count": 0,
            "message": "Keine prüfbaren Container-Namen.",
        }

    quoted = " ".join(shlex.quote(n) for n in names)
    stdout, stderr, code = await _ssh_run(
        settings,
        ip,
        "docker inspect -- " + quoted,
        port=port,
        username=user,
        cmd_timeout=inspect_timeout,
    )
    if code != 0:
        raw = (stderr or stdout or "").strip() or f"exit {code}"
        raise DockerControlError(
            f"docker inspect fehlgeschlagen: {_unique_cmd_detail(raw, fallback=raw)}",
            status_code=502,
        )

    inspected = _parse_inspect_payload(stdout)
    if not inspected:
        raise DockerControlError(
            "docker inspect JSON ungültig.",
            status_code=502,
        )

    specs: list[tuple[str, str, str]] = []
    image_refs: list[str] = []
    for item in inspected:
        cname = str(item.get("Name") or "").strip().lstrip("/")
        config = item.get("Config") if isinstance(item.get("Config"), dict) else {}
        image = str(config.get("Image") or "").strip()
        image_id = str(item.get("Image") or "").strip()
        if not cname:
            continue
        specs.append((cname, image, image_id))
        if image_id:
            image_refs.append(image_id)
        if image and not image.startswith("sha256:"):
            image_refs.append(image)

    digest_by_ref = await _inspect_image_repo_digests(
        settings,
        ip,
        image_refs,
        port=port,
        username=user,
        cmd_timeout=inspect_timeout,
    )

    rows: list[dict[str, Any]] = []
    for cname, image, image_id in specs:
        local_digests = digest_by_ref.get(image_id) or digest_by_ref.get(image) or []
        local_sha = _local_sha_from_digests(local_digests)
        remote_sha = ""
        newer = False
        err = ""
        if not image or image.startswith("sha256:"):
            err = "Kein Tag — Vergleich nicht möglich."
        elif not local_sha:
            err = _LOCAL_ONLY_STATUS
        else:
            remote_cmd = (
                "docker buildx imagetools inspect --format '{{.Manifest.Digest}}' "
                + shlex.quote(image)
                + " 2>/dev/null || docker manifest inspect --verbose "
                + shlex.quote(image)
                + " 2>/dev/null | head -c 400"
            )
            r_out, r_err, r_code = await _ssh_run(
                settings,
                ip,
                remote_cmd,
                port=port,
                username=user,
                cmd_timeout=inspect_timeout,
            )
            text = (r_out or "").strip()
            if r_code == 0 and "sha256:" in text:
                for token in text.replace('"', " ").replace("'", " ").split():
                    if token.startswith("sha256:"):
                        remote_sha = token.split(",", 1)[0]
                        break
            if not remote_sha:
                err = (r_err or text or "Remote-Manifest nicht lesbar").strip()[:180]
            elif local_sha != remote_sha:
                newer = True
        stack = ""
        for c in containers:
            if c.name == cname or c.name.lstrip("/") == cname:
                stack = (c.labels or {}).get("com.docker.compose.project") or ""
                break
        rows.append(
            {
                "name": cname,
                "image": image,
                "stack": stack,
                "local_digest": local_sha,
                "remote_digest": remote_sha,
                "update_available": newer,
                "error": err,
            }
        )

    updates = [r for r in rows if r["update_available"]]
    local_only = [r for r in rows if r["error"] == _LOCAL_ONLY_STATUS]
    if updates:
        message = f"{len(updates)} Image-Update(s) verfügbar."
    elif local_only and len(local_only) == len(rows):
        message = "Keine neueren Images — alle nur lokal (Remote-Vergleich nicht möglich)."
    elif local_only:
        message = (
            f"Keine neueren Images gefunden. {len(local_only)} Image(s) nur lokal "
            "— Remote-Vergleich nicht möglich."
        )
    else:
        message = "Keine neueren Images gefunden."
    return {
        "ok": True,
        "parent_id": parent_id,
        "project": project,
        "containers": rows,
        "updates": updates,
        "count": len(updates),
        "message": message,
    }


async def apply_image_updates(
    settings: Settings,
    *,
    parent_id: str,
    snapshot: TopologySnapshot | None,
    project: str | None = None,
    names: list[str] | None = None,
    restart: bool = True,
    pull_timeout: float = 1800.0,
    on_line: Callable[[str], Awaitable[None] | None] | None = None,
) -> dict[str, Any]:
    """Pull newer images after explicit confirmation. Restart only if requested."""
    ip, port, user = resolve_parent_ssh(settings, snapshot, parent_id)
    if ip is None:
        raise DockerControlError(
            "Lokales Image-Update nicht vorgesehen.",
            status_code=400,
        )
    if not ssh_key_present(settings):
        raise DockerControlError(
            "SSH-Schlüssel fehlt — Pull nicht möglich.",
            status_code=503,
        )

    async def _run(cmd: str, *, stream: bool = False) -> tuple[str, str, int]:
        if stream or on_line:
            return await _ssh_run_stream(
                settings,
                ip,
                cmd,
                port=port,
                username=user,
                cmd_timeout=pull_timeout,
                on_line=on_line,
            )
        return await _ssh_run(
            settings,
            ip,
            cmd,
            port=port,
            username=user,
            cmd_timeout=pull_timeout,
        )

    safe_names = [validate_docker_name(n) for n in (names or []) if n]
    project = (project or "").strip() or None
    if project:
        validate_docker_name(project, kind="Compose-Projekt")

    logs: list[str] = []
    if project:
        cmd = f"docker compose -p {shlex.quote(project)} pull"
        stdout, stderr, code = await _run(cmd, stream=True)
        logs.append((stdout or stderr or "").strip()[:4000])
        if code != 0:
            raise DockerControlError(
                f"docker compose pull fehlgeschlagen: {(stderr or stdout or '').strip()[:240]}",
                status_code=502,
            )
        if restart:
            rcmd = f"docker compose -p {shlex.quote(project)} up -d"
            stdout, stderr, code = await _run(rcmd, stream=True)
            logs.append((stdout or stderr or "").strip()[:2000])
            if code != 0:
                raise DockerControlError(
                    f"Stack-Neustart fehlgeschlagen: {(stderr or stdout or '').strip()[:240]}",
                    status_code=502,
                )
        return {
            "ok": True,
            "parent_id": parent_id,
            "project": project,
            "restarted": restart,
            "log": "\n".join(x for x in logs if x),
            "message": (
                f"Stack „{project}“ Images geholt"
                + (" und neu gestartet." if restart else " (ohne Neustart).")
            ),
        }

    if not safe_names:
        raise DockerControlError(
            "Bitte Container-Namen oder ein Compose-Projekt angeben.",
            status_code=400,
        )
    pulled: list[str] = []
    for name in safe_names:
        img_cmd = (
            "docker inspect --format '{{.Config.Image}}' -- " + shlex.quote(name)
        )
        stdout, stderr, code = await _ssh_run(
            settings,
            ip,
            img_cmd,
            port=port,
            username=user,
            cmd_timeout=min(60.0, pull_timeout),
        )
        image = (stdout or "").strip()
        if code != 0 or not image:
            raise DockerControlError(
                f"Image für „{name}“ nicht lesbar: {(stderr or stdout or '').strip()[:180]}",
                status_code=502,
            )
        stdout, stderr, code = await _run(
            f"docker pull -- {shlex.quote(image)}", stream=True
        )
        logs.append((stdout or stderr or "").strip()[:2000])
        if code != 0:
            raise DockerControlError(
                f"docker pull {image} fehlgeschlagen: {(stderr or stdout or '').strip()[:240]}",
                status_code=502,
            )
        pulled.append(image)
        if restart:
            stdout, stderr, code = await _run(
                f"docker restart -- {shlex.quote(name)}", stream=True
            )
            logs.append((stdout or stderr or "").strip()[:800])
            if code != 0:
                raise DockerControlError(
                    f"Neustart „{name}“ fehlgeschlagen: {(stderr or stdout or '').strip()[:200]}",
                    status_code=502,
                )
    return {
        "ok": True,
        "parent_id": parent_id,
        "images": pulled,
        "restarted": restart,
        "log": "\n".join(x for x in logs if x),
        "message": (
            f"{len(pulled)} Image(s) geholt"
            + (" und Container neu gestartet." if restart else " (ohne Neustart).")
        ),
    }
