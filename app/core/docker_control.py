"""Docker start/stop/restart/logs via SSH (or local socket)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shlex
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
) -> tuple[str, str, int]:
    key = ssh_key_path(settings)
    connect_timeout = min(3.0, settings.docker_ssh_timeout + 1.0)
    cmd_timeout = max(5.0, settings.docker_ssh_timeout + 10.0)
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


async def scan_image_updates(
    settings: Settings,
    *,
    parent_id: str,
    snapshot: TopologySnapshot | None,
    project: str | None = None,
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
    inspect_cmd = (
        "docker inspect --format "
        "'{{.Name}}|{{.Config.Image}}|{{json .RepoDigests}}' -- " + quoted
    )
    stdout, stderr, code = await _ssh_run(
        settings, ip, inspect_cmd, port=port, username=user
    )
    if code != 0:
        detail = (stderr or stdout or "").strip() or f"exit {code}"
        raise DockerControlError(
            f"docker inspect fehlgeschlagen: {detail}",
            status_code=502,
        )

    rows: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        line = line.strip().lstrip("/")
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        cname, image, digests_raw = parts[0].strip(), parts[1].strip(), parts[2].strip()
        local_digests: list[str] = []
        try:
            parsed = json.loads(digests_raw)
            if isinstance(parsed, list):
                local_digests = [str(x) for x in parsed if x]
        except json.JSONDecodeError:
            local_digests = []
        local_sha = ""
        for d in local_digests:
            if "@sha256:" in d:
                local_sha = d.split("@", 1)[-1]
                break
        remote_sha = ""
        newer = False
        err = ""
        if not image or image.startswith("sha256:"):
            err = "Kein Tag — Vergleich nicht möglich."
        else:
            remote_cmd = (
                "docker buildx imagetools inspect --format '{{.Manifest.Digest}}' "
                + shlex.quote(image)
                + " 2>/dev/null || docker manifest inspect --verbose "
                + shlex.quote(image)
                + " 2>/dev/null | head -c 400"
            )
            r_out, r_err, r_code = await _ssh_run(
                settings, ip, remote_cmd, port=port, username=user
            )
            text = (r_out or "").strip()
            if r_code == 0 and "sha256:" in text:
                for token in text.replace('"', " ").replace("'", " ").split():
                    if token.startswith("sha256:"):
                        remote_sha = token.split(",", 1)[0]
                        break
            if not remote_sha:
                err = (
                    (r_err or text or "Remote-Manifest nicht lesbar").strip()[:180]
                )
            elif local_sha and remote_sha and local_sha != remote_sha:
                newer = True
            elif not local_sha and remote_sha:
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
    return {
        "ok": True,
        "parent_id": parent_id,
        "project": project,
        "containers": rows,
        "updates": updates,
        "count": len(updates),
        "message": (
            f"{len(updates)} Image-Update(s) verfügbar."
            if updates
            else "Keine neueren Images gefunden."
        ),
    }


async def apply_image_updates(
    settings: Settings,
    *,
    parent_id: str,
    snapshot: TopologySnapshot | None,
    project: str | None = None,
    names: list[str] | None = None,
    restart: bool = True,
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

    safe_names = [validate_docker_name(n) for n in (names or []) if n]
    project = (project or "").strip() or None
    if project:
        validate_docker_name(project, kind="Compose-Projekt")

    logs: list[str] = []
    if project:
        cmd = f"docker compose -p {shlex.quote(project)} pull"
        stdout, stderr, code = await _ssh_run(
            settings, ip, cmd, port=port, username=user
        )
        logs.append((stdout or stderr or "").strip()[:4000])
        if code != 0:
            raise DockerControlError(
                f"docker compose pull fehlgeschlagen: {(stderr or stdout or '').strip()[:240]}",
                status_code=502,
            )
        if restart:
            rcmd = f"docker compose -p {shlex.quote(project)} up -d"
            stdout, stderr, code = await _ssh_run(
                settings, ip, rcmd, port=port, username=user
            )
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
            settings, ip, img_cmd, port=port, username=user
        )
        image = (stdout or "").strip()
        if code != 0 or not image:
            raise DockerControlError(
                f"Image für „{name}“ nicht lesbar: {(stderr or stdout or '').strip()[:180]}",
                status_code=502,
            )
        stdout, stderr, code = await _ssh_run(
            settings, ip, f"docker pull -- {shlex.quote(image)}", port=port, username=user
        )
        logs.append((stdout or stderr or "").strip()[:2000])
        if code != 0:
            raise DockerControlError(
                f"docker pull {image} fehlgeschlagen: {(stderr or stdout or '').strip()[:240]}",
                status_code=502,
            )
        pulled.append(image)
        if restart:
            stdout, stderr, code = await _ssh_run(
                settings,
                ip,
                f"docker restart -- {shlex.quote(name)}",
                port=port,
                username=user,
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
