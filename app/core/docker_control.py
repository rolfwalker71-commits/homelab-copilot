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
from app.core.compose_apply import (
    absolute_compose_path,
    cmd_compose_ls_json,
    cmd_find_common_compose_files,
    compose_ls_match,
    compose_skip_reason_de,
    compose_spec_from_inspects,
    pull_fail_message_de,
    compose_stack_argv,
    compose_stack_shell,
    docker_create_argv_from_inspect,
    extra_networks_from_inspect,
    inspect_container_image,
    inspect_container_name,
    normalize_guest_path,
    parse_compose_ls_json,
)
from app.core.image_digest import (
    cmd_compose_project_labels,
    cmd_remote_manifest_inspect,
    first_digest,
    image_digest_status,
    local_digest_set,
    parse_compose_project_label_lines,
    parse_remote_inspect_digests,
)
from app.core.image_prune import (
    cmd_dangling_image_prune,
    cmd_in_use_image_ids,
    cmd_named_image_ids,
    cmd_project_image_ids,
    cmd_rmi_unused,
    compose_projects_for_container_names,
    format_unused_image_cleanup_message,
    parse_image_id_lines,
    parse_image_prune_output,
    replaced_image_ids_to_remove,
)
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
    spec = compose_spec_from_inspects(inspects)
    working_dir = spec["working_dir"]
    config_files: list[str] = []
    for item in spec["config_files"]:
        abs_path = absolute_compose_path(working_dir, item)
        if abs_path and abs_path not in config_files:
            config_files.append(abs_path)
    return {
        "working_dir": working_dir,
        "config_files": config_files,
        "candidates": list(spec["candidates"]),
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
    port: int | None = None,
    username: str | None = None,
) -> list[str]:
    existing: list[str] = []
    # Prefer label-declared files first.
    ordered = [p for p in candidates if p in config_files] + [
        p for p in candidates if p not in config_files
    ]
    for path in ordered:
        if await _remote_or_local_readable(
            settings, ip, path, port=port, username=username
        ):
            if path not in existing:
                existing.append(path)
    return existing


async def _remote_or_local_readable(
    settings: Settings,
    ip: str | None,
    path: str,
    *,
    port: int | None = None,
    username: str | None = None,
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
    _, _, code = await _ssh_run(settings, ip, check, port=port, username=username)
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
    _ip, port, user = (
        resolve_parent_ssh(settings, snapshot, parent_id)
        if ip
        else (None, settings.docker_ssh_port, settings.docker_ssh_user)
    )
    existing = await _resolve_existing_compose_paths(
        settings,
        ip=ip,
        candidates=meta["candidates"],
        config_files=meta["config_files"],
        port=port,
        username=user,
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
    """Compare local RepoDigests / image id vs registry manifests (no pull)."""
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
        raw_local = digest_by_ref.get(image_id) or digest_by_ref.get(image) or []
        local_set = local_digest_set(repo_digests=raw_local, image_id=image_id)
        local_sha = first_digest(local_set) or _local_sha_from_digests(raw_local)
        remote_set: set[str] = set()
        remote_sha = ""
        newer = False
        err = ""
        if not image or image.startswith("sha256:"):
            err = "Kein Tag — Vergleich nicht möglich."
        else:
            r_out, r_err, r_code = await _ssh_run(
                settings,
                ip,
                cmd_remote_manifest_inspect(image),
                port=port,
                username=user,
                cmd_timeout=inspect_timeout,
            )
            text = (r_out or "").strip()
            if r_code == 0:
                remote_set = parse_remote_inspect_digests(text)
                remote_sha = first_digest(remote_set)
            status = image_digest_status(local_set, remote_set)
            if status == "update":
                newer = True
            elif status == "unknown":
                remote_err = (r_err or "").strip()
                if remote_err and "sha256:" not in remote_err.lower():
                    err = remote_err[:180]
                else:
                    err = _LOCAL_ONLY_STATUS
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


def _compose_name_to_project(
    snapshot: TopologySnapshot | None, parent_id: str
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if snapshot is None:
        return mapping
    for container in snapshot.containers:
        if container.parent_id != parent_id:
            continue
        project = (container.labels or {}).get("com.docker.compose.project") or ""
        if not project:
            continue
        mapping[container.name] = project
        mapping[container.name.lstrip("/")] = project
    return mapping


async def _guest_path_exists(
    settings: Settings,
    ip: str,
    path: str,
    *,
    port: int | None,
    username: str | None,
) -> bool:
    return await _remote_or_local_readable(
        settings, ip, path, port=port, username=username
    )


async def _resolve_stack_compose_on_guest(
    settings: Settings,
    *,
    ip: str,
    project: str,
    inspects: list[dict[str, Any]],
    port: int | None,
    username: str | None,
    emit: Callable[[str], Awaitable[None] | None],
    run_quiet: Callable[[str], Awaitable[tuple[str, str, int]]],
) -> dict[str, Any]:
    """Find compose files on the Docker guest. Empty config_files → skip compose."""
    spec = compose_spec_from_inspects(inspects, project=project)
    working_dir = spec["working_dir"]

    async def _exists(path: str) -> bool:
        return await _guest_path_exists(
            settings, ip, path, port=port, username=username
        )

    existing_labeled: list[str] = []
    for item in spec["config_files"]:
        abs_path = absolute_compose_path(working_dir, item)
        if abs_path and await _exists(abs_path):
            existing_labeled.append(item)

    if existing_labeled:
        wd = working_dir
        if not wd:
            first = absolute_compose_path(None, existing_labeled[0]) or absolute_compose_path(
                working_dir, existing_labeled[0]
            )
            wd = first.rsplit("/", 1)[0] if first else None
        return {
            "working_dir": wd,
            "config_files": existing_labeled,
            "source": "labels",
        }

    if working_dir:
        found: list[str] = []
        for name in _COMPOSE_BASENAMES:
            if await _exists(f"{working_dir}/{name}"):
                found.append(name)
        if found:
            return {
                "working_dir": working_dir,
                "config_files": found,
                "source": "working_dir",
            }

    await emit(
        f"Working-Dir fehlt oder Dateien nicht lesbar für „{project}“ — "
        f"prüfe /opt/{project}, /home/*/docker/{project} und docker compose ls."
    )

    stdout, _stderr, code = await run_quiet(cmd_find_common_compose_files(project))
    if code == 0:
        by_dir: dict[str, list[str]] = {}
        for line in stdout.splitlines():
            norm = normalize_guest_path(line.strip())
            if not norm:
                continue
            directory, name = norm.rsplit("/", 1)
            if name not in by_dir.setdefault(directory, []):
                by_dir[directory].append(name)
        prefer = f"/opt/{project}"
        if prefer in by_dir:
            return {
                "working_dir": prefer,
                "config_files": by_dir[prefer],
                "source": "common_path",
            }
        if by_dir:
            directory = next(iter(by_dir))
            return {
                "working_dir": directory,
                "config_files": by_dir[directory],
                "source": "common_path",
            }

    stdout, _stderr, code = await run_quiet(cmd_compose_ls_json())
    if code == 0:
        wd, files = compose_ls_match(parse_compose_ls_json(stdout), project)
        existing: list[str] = []
        for item in files:
            abs_path = absolute_compose_path(wd, item)
            if abs_path and await _exists(abs_path):
                existing.append(item)
        if existing:
            if not wd:
                first = absolute_compose_path(wd, existing[0])
                wd = first.rsplit("/", 1)[0] if first else None
            return {
                "working_dir": wd,
                "config_files": existing,
                "source": "compose_ls",
            }

    return {"working_dir": None, "config_files": [], "source": "none"}


def _compose_apply_shell(
    *,
    project: str,
    working_dir: str | None,
    config_files: list[str],
    extra: list[str],
) -> str:
    argv = compose_stack_argv(
        project=project, config_files=config_files, extra=extra
    )
    return compose_stack_shell(working_dir=working_dir, argv=argv)


async def _pull_and_recreate_without_compose(
    *,
    inspects: list[dict[str, Any]],
    restart: bool,
    run: Callable[..., Awaitable[tuple[str, str, int]]],
    emit: Callable[[str], Awaitable[None] | None],
) -> None:
    """docker pull each image; recreate containers when restart is requested."""
    if not inspects:
        raise DockerControlError(
            "Keine Container für das Compose-Projekt gefunden — "
            "weder Compose-Datei noch inspect-Daten.",
            status_code=404,
        )

    images: list[str] = []
    for info in inspects:
        image = inspect_container_image(info)
        if image and image not in images and not image.startswith("sha256:"):
            images.append(image)
    if not images:
        raise DockerControlError(
            "Keine Image-Tags zum Ziehen (nur sha256-IDs) — "
            "ohne Compose-Datei nicht aktualisierbar.",
            status_code=502,
        )

    for image in images:
        await emit(f"Hole Image {image} (ohne docker compose)…")
        stdout, stderr, code = await run(
            f"docker pull -- {shlex.quote(image)}", stream=True
        )
        if code != 0:
            raise DockerControlError(
                pull_fail_message_de(
                    (stderr or stdout or "").strip(), compose=False
                ),
                status_code=502,
            )

    if not restart:
        return

    for info in inspects:
        await _recreate_container_from_inspect(info, run=run, emit=emit)


async def _recreate_container_from_inspect(
    info: dict[str, Any],
    *,
    run: Callable[..., Awaitable[tuple[str, str, int]]],
    emit: Callable[[str], Awaitable[None] | None],
) -> None:
    name = inspect_container_name(info)
    image = inspect_container_image(info)
    if not name or not _SAFE_NAME.match(name):
        raise DockerControlError(
            f"Container-Name ungültig für Recreate: {name!r}.",
            status_code=400,
        )
    if not image or image.startswith("sha256:"):
        raise DockerControlError(
            f"Kein Image-Tag für „{name}“ — Recreate nicht möglich.",
            status_code=502,
        )
    old = f"{name}.hlops-old"
    create_cmd = " ".join(
        shlex.quote(part) for part in docker_create_argv_from_inspect(info)
    )
    await emit(f"Erzeuge Container „{name}“ neu (ohne Compose-Datei).")
    await run(f"docker rm -f -- {shlex.quote(old)}")
    stdout, stderr, code = await run(
        f"docker stop -- {shlex.quote(name)}", stream=True
    )
    if code != 0:
        raise DockerControlError(
            f"Stop „{name}“ fehlgeschlagen: {(stderr or stdout or '').strip()[:200]}",
            status_code=502,
        )
    stdout, stderr, code = await run(
        f"docker rename {shlex.quote(name)} {shlex.quote(old)}"
    )
    if code != 0:
        await run(f"docker start -- {shlex.quote(name)}")
        raise DockerControlError(
            f"Rename „{name}“ fehlgeschlagen: {(stderr or stdout or '').strip()[:200]}",
            status_code=502,
        )

    stdout, stderr, code = await run(create_cmd, stream=True)
    if code != 0:
        await run(f"docker rename {shlex.quote(old)} {shlex.quote(name)}")
        await run(f"docker start -- {shlex.quote(name)}")
        raise DockerControlError(
            f"Recreate „{name}“ fehlgeschlagen: {(stderr or stdout or '').strip()[:240]}",
            status_code=502,
        )

    for net in extra_networks_from_inspect(info):
        if not _SAFE_NAME.match(net):
            continue
        await run(
            f"docker network connect {shlex.quote(net)} {shlex.quote(name)}"
        )

    stdout, stderr, code = await run(
        f"docker start -- {shlex.quote(name)}", stream=True
    )
    if code != 0:
        await run(f"docker rm -f -- {shlex.quote(name)}")
        await run(f"docker rename {shlex.quote(old)} {shlex.quote(name)}")
        await run(f"docker start -- {shlex.quote(name)}")
        raise DockerControlError(
            f"Start nach Recreate „{name}“ fehlgeschlagen: "
            f"{(stderr or stdout or '').strip()[:200]}",
            status_code=502,
        )
    await run(f"docker rm -- {shlex.quote(old)}")


async def apply_image_updates(
    settings: Settings,
    *,
    parent_id: str,
    snapshot: TopologySnapshot | None,
    project: str | None = None,
    names: list[str] | None = None,
    restart: bool = True,
    prune: bool = True,
    pull_timeout: float = 1800.0,
    on_line: Callable[[str], Awaitable[None] | None] | None = None,
) -> dict[str, Any]:
    """Pull newer images after explicit confirmation. Restart only if requested.

    After a successful recreate (compose up), unused images from this upgrade
    are pruned best-effort — a prune failure does not fail the upgrade.
    """
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

    async def _run_quiet(
        cmd: str, *, cmd_timeout: float | None = None
    ) -> tuple[str, str, int]:
        return await _ssh_run(
            settings,
            ip,
            cmd,
            port=port,
            username=user,
            cmd_timeout=cmd_timeout if cmd_timeout is not None else min(90.0, pull_timeout),
        )

    logs: list[str] = []

    async def _emit(text: str) -> None:
        line = (text or "").strip()
        if not line:
            return
        logs.append(line)
        if not on_line:
            return
        result = on_line(line)
        if asyncio.iscoroutine(result):
            await result

    async def _collect_ids(cmd: str) -> set[str]:
        stdout, _stderr, code = await _run_quiet(cmd)
        if code != 0:
            return set()
        return parse_image_id_lines(stdout)

    async def _cleanup_unused_images(
        *,
        previous_ids: set[str],
        current_ids_cmd: str,
    ) -> dict[str, Any]:
        """Dangling prune + unused previous tags. Never fails the upgrade."""
        try:
            await _emit("Ungenutzte Images entfernen…")
            current_ids = await _collect_ids(current_ids_cmd)
            in_use_ids = await _collect_ids(cmd_in_use_image_ids())
            to_remove = replaced_image_ids_to_remove(
                previous_ids=previous_ids,
                current_ids=current_ids,
                in_use_ids=in_use_ids,
            )
            replaced_removed = 0
            rmi_cmd = cmd_rmi_unused(to_remove)
            if rmi_cmd:
                stdout, stderr, code = await _run_quiet(rmi_cmd, cmd_timeout=min(180.0, pull_timeout))
                parsed_rmi = parse_image_prune_output(f"{stdout or ''}\n{stderr or ''}")
                if code == 0:
                    replaced_removed = len(to_remove)
                else:
                    replaced_removed = int(parsed_rmi["untagged"] or 0)

            stdout, stderr, pcode = await _run_quiet(
                cmd_dangling_image_prune(),
                cmd_timeout=min(180.0, pull_timeout),
            )
            prune_text = f"{stdout or ''}\n{stderr or ''}"
            parsed = parse_image_prune_output(prune_text)
            warning = None
            if pcode != 0:
                warning = (stderr or stdout or f"exit {pcode}").strip()[:180]
            message = format_unused_image_cleanup_message(
                dangling_deleted=int(parsed["deleted"] or 0),
                dangling_untagged=int(parsed["untagged"] or 0),
                replaced_removed=replaced_removed,
                reclaimed=str(parsed.get("reclaimed") or ""),
                warning=warning,
            )
            await _emit(message)
            return {
                "ok": warning is None,
                "deleted": int(parsed["deleted"] or 0) + replaced_removed,
                "reclaimed": parsed.get("reclaimed") or "",
                "message": message,
                "warning": warning,
            }
        except DockerControlError as exc:
            message = format_unused_image_cleanup_message(warning=exc.message)
            await _emit(message)
            return {
                "ok": False,
                "deleted": 0,
                "reclaimed": "",
                "message": message,
                "warning": exc.message,
            }
        except Exception as exc:
            message = format_unused_image_cleanup_message(warning=str(exc))
            await _emit(message)
            logger.warning("image prune after upgrade failed: %s", exc)
            return {
                "ok": False,
                "deleted": 0,
                "reclaimed": "",
                "message": message,
                "warning": str(exc),
            }

    safe_names = [validate_docker_name(n) for n in (names or []) if n]
    project = (project or "").strip() or None
    if project:
        validate_docker_name(project, kind="Compose-Projekt")

    stacks_to_upgrade: list[str] = []
    names_to_upgrade: list[str] = list(safe_names)
    if project:
        stacks_to_upgrade = [project]
        names_to_upgrade = []
    elif safe_names:
        name_to_project = _compose_name_to_project(snapshot, parent_id)
        live_out, _live_err, live_code = await _run_quiet(
            cmd_compose_project_labels(safe_names)
        )
        if live_code == 0:
            name_to_project.update(parse_compose_project_label_lines(live_out))
        projects, leftover = compose_projects_for_container_names(
            name_to_project, safe_names
        )
        valid_stacks: list[str] = []
        for stack_name in projects:
            try:
                valid_stacks.append(
                    validate_docker_name(stack_name, kind="Compose-Projekt")
                )
            except DockerControlError:
                for name in safe_names:
                    mapped = (
                        name_to_project.get(name)
                        or name_to_project.get(name.lstrip("/"))
                        or ""
                    )
                    if mapped == stack_name and name not in leftover:
                        leftover.append(name)
        stacks_to_upgrade = valid_stacks
        names_to_upgrade = leftover

    if not stacks_to_upgrade and not names_to_upgrade:
        raise DockerControlError(
            "Bitte Container-Namen oder ein Compose-Projekt angeben.",
            status_code=400,
        )

    prune_results: list[dict[str, Any]] = []
    pulled: list[str] = []
    last_project: str | None = project

    for stack in stacks_to_upgrade:
        last_project = stack
        previous_ids: set[str] = set()
        if prune and restart:
            previous_ids = await _collect_ids(cmd_project_image_ids(stack))
        inspects = await _remote_inspect_project(
            settings, ip, stack, port=port, username=user
        )
        resolved = await _resolve_stack_compose_on_guest(
            settings,
            ip=ip,
            project=stack,
            inspects=inspects,
            port=port,
            username=user,
            emit=_emit,
            run_quiet=_run_quiet,
        )
        compose_files = list(resolved.get("config_files") or [])
        compose_cwd = resolved.get("working_dir")

        if compose_files:
            file_note = ", ".join(compose_files)
            cwd_note = compose_cwd or "ohne Working-Dir"
            await _emit(
                f"Compose-Projekt „{stack}“: {cwd_note}, Dateien: {file_note}."
            )
            cmd = _compose_apply_shell(
                project=stack,
                working_dir=compose_cwd,
                config_files=compose_files,
                extra=["pull"],
            )
            stdout, stderr, code = await _run(cmd, stream=True)
            logs.append((stdout or stderr or "").strip()[:4000])
            if code != 0:
                raise DockerControlError(
                    pull_fail_message_de(
                        (stderr or stdout or "").strip(), compose=True
                    ),
                    status_code=502,
                )
            if restart:
                rcmd = _compose_apply_shell(
                    project=stack,
                    working_dir=compose_cwd,
                    config_files=compose_files,
                    extra=["up", "-d", "--force-recreate"],
                )
                stdout, stderr, code = await _run(rcmd, stream=True)
                logs.append((stdout or stderr or "").strip()[:2000])
                if code != 0:
                    raise DockerControlError(
                        f"Stack-Neustart fehlgeschlagen: {(stderr or stdout or '').strip()[:240]}",
                        status_code=502,
                    )
        else:
            await _emit(compose_skip_reason_de())
            await _pull_and_recreate_without_compose(
                inspects=inspects,
                restart=restart,
                run=_run,
                emit=_emit,
            )

        if restart and prune:
            prune_results.append(
                await _cleanup_unused_images(
                    previous_ids=previous_ids,
                    current_ids_cmd=cmd_project_image_ids(stack),
                )
            )

    if names_to_upgrade:
        previous_ids = set()
        if prune and restart:
            previous_ids = await _collect_ids(cmd_named_image_ids(names_to_upgrade))
        for name in names_to_upgrade:
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
                    pull_fail_message_de(
                        (stderr or stdout or "").strip(), compose=False
                    ),
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
        if prune and restart:
            prune_results.append(
                await _cleanup_unused_images(
                    previous_ids=previous_ids,
                    current_ids_cmd=cmd_named_image_ids(names_to_upgrade),
                )
            )

    prune_info = prune_results[-1] if len(prune_results) == 1 else None
    if len(prune_results) > 1:
        warnings = [p.get("warning") for p in prune_results if p.get("warning")]
        deleted = sum(int(p.get("deleted") or 0) for p in prune_results)
        reclaimed = next(
            (p.get("reclaimed") for p in reversed(prune_results) if p.get("reclaimed")),
            "",
        )
        warning = "; ".join(str(w) for w in warnings if w) or None
        message = (
            format_unused_image_cleanup_message(warning=warning)
            if warning
            else format_unused_image_cleanup_message(
                dangling_deleted=deleted,
                reclaimed=str(reclaimed or ""),
            )
        )
        prune_info = {
            "ok": warning is None,
            "deleted": deleted,
            "reclaimed": reclaimed or "",
            "message": message,
            "warning": warning,
        }

    if stacks_to_upgrade and not names_to_upgrade:
        stack_label = stacks_to_upgrade[0] if len(stacks_to_upgrade) == 1 else None
        if stack_label:
            base = (
                f"Stack „{stack_label}“ Images geholt"
                + (" und neu gestartet." if restart else " (ohne Neustart).")
            )
        else:
            base = (
                f"{len(stacks_to_upgrade)} Stack(s) Images geholt"
                + (" und neu gestartet." if restart else " (ohne Neustart).")
            )
        if prune_info and prune_info.get("message"):
            base = f"{base} {prune_info['message']}"
        return {
            "ok": True,
            "parent_id": parent_id,
            "project": last_project or stack_label,
            "projects": stacks_to_upgrade,
            "restarted": restart,
            "pruned": bool(prune and restart),
            "prune": prune_info,
            "log": "\n".join(x for x in logs if x),
            "message": base,
        }

    message = (
        f"{len(pulled)} Image(s) geholt"
        + (" und Container neu gestartet." if restart else " (ohne Neustart).")
    )
    if stacks_to_upgrade:
        message = (
            f"{len(stacks_to_upgrade)} Stack(s) und {len(pulled)} Image(s) geholt"
            + (" und neu gestartet." if restart else " (ohne Neustart).")
        )
    if prune_info and prune_info.get("message"):
        message = f"{message} {prune_info['message']}"
    return {
        "ok": True,
        "parent_id": parent_id,
        "project": last_project,
        "projects": stacks_to_upgrade,
        "images": pulled,
        "restarted": restart,
        "pruned": bool(prune and restart),
        "prune": prune_info,
        "log": "\n".join(x for x in logs if x),
        "message": message,
    }
