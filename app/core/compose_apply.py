"""Resolve Compose project files and build docker compose argv for image apply.

Compose v2 stores the project directory and file list on each container:
``com.docker.compose.project.working_dir``,
``com.docker.compose.project.config_files``,
``com.docker.compose.project``.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any

LABEL_PROJECT = "com.docker.compose.project"
LABEL_WORKING_DIR = "com.docker.compose.project.working_dir"
LABEL_CONFIG_FILES = "com.docker.compose.project.config_files"

COMPOSE_BASENAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)

_COMPOSE_SKIP_DE = (
    "Keine Compose-Datei auf dem Gast gefunden "
    "(Working-Dir fehlt, Verzeichnis gelöscht oder nur Portainer). "
    "docker compose wird übersprungen — Images per docker pull, "
    "Container werden neu erzeugt."
)


def normalize_guest_path(path: str) -> str | None:
    """Absolute guest path without ``..``. None if empty or unsafe."""
    raw = (path or "").strip()
    if not raw or "\x00" in raw or not raw.startswith("/"):
        return None
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/" + "/".join(parts)


def parse_compose_config_files(raw: str) -> list[str]:
    """Split Compose v2 ``config_files`` (comma, sometimes colon)."""
    files: list[str] = []
    for part in re.split(r"[,:]", raw or ""):
        part = part.strip()
        if part and part not in files:
            files.append(part)
    return files


def absolute_compose_path(working_dir: str | None, file: str) -> str | None:
    """Resolve a labeled compose path against working_dir for existence checks."""
    name = (file or "").strip()
    if not name:
        return None
    if name.startswith("/"):
        return normalize_guest_path(name)
    wd = normalize_guest_path((working_dir or "").rstrip("/")) if working_dir else None
    if not wd:
        return None
    return normalize_guest_path(f"{wd}/{name}")


def compose_spec_from_labels(labels: dict[str, str] | None) -> dict[str, Any]:
    """Extract project / working_dir / config_files from Compose v2 labels."""
    src = labels or {}
    project = (src.get(LABEL_PROJECT) or "").strip()
    working_dir = normalize_guest_path((src.get(LABEL_WORKING_DIR) or "").rstrip("/"))
    raw_files = parse_compose_config_files(src.get(LABEL_CONFIG_FILES) or "")
    config_files: list[str] = []
    candidates: list[str] = []
    for item in raw_files:
        if item not in config_files:
            config_files.append(item)
        abs_path = absolute_compose_path(working_dir, item)
        if abs_path and abs_path not in candidates:
            candidates.append(abs_path)
    if working_dir:
        for name in COMPOSE_BASENAMES:
            guess = f"{working_dir}/{name}"
            if guess not in candidates:
                candidates.append(guess)
    return {
        "project": project,
        "working_dir": working_dir,
        "config_files": config_files,
        "candidates": candidates,
    }


def compose_spec_from_inspects(
    inspects: list[dict[str, Any]], *, project: str = ""
) -> dict[str, Any]:
    """Merge Compose labels from ``docker inspect`` payloads."""
    merged: dict[str, str] = {}
    if project:
        merged[LABEL_PROJECT] = project
    for info in inspects:
        labels = (info.get("Config") or {}).get("Labels") or {}
        if not isinstance(labels, dict):
            continue
        for key in (LABEL_PROJECT, LABEL_WORKING_DIR, LABEL_CONFIG_FILES):
            value = labels.get(key)
            if value and key not in merged:
                merged[key] = str(value)
            elif key == LABEL_CONFIG_FILES and value:
                existing = merged.get(key) or ""
                for part in parse_compose_config_files(str(value)):
                    if part not in parse_compose_config_files(existing):
                        existing = f"{existing},{part}" if existing else part
                merged[key] = existing
    spec = compose_spec_from_labels(merged)
    if project and not spec["project"]:
        spec["project"] = project
    return spec


def compose_file_argv(config_files: list[str]) -> list[str]:
    """``['-f', 'a.yml', '-f', 'b.yml']`` for files that should be passed to Compose."""
    argv: list[str] = []
    for item in config_files:
        name = (item or "").strip()
        if name:
            argv.extend(["-f", name])
    return argv


def compose_stack_argv(
    *,
    project: str,
    config_files: list[str],
    extra: list[str],
) -> list[str]:
    """``docker compose -p PROJECT -f a.yml -f b.yml …`` (no cwd)."""
    return [
        "docker",
        "compose",
        "-p",
        project,
        *compose_file_argv(config_files),
        *extra,
    ]


def compose_stack_shell(*, working_dir: str | None, argv: list[str]) -> str:
    """Shell command: ``cd working_dir && docker compose …`` when cwd is known."""
    cmd = " ".join(shlex.quote(part) for part in argv)
    wd = normalize_guest_path((working_dir or "").rstrip("/")) if working_dir else None
    if wd:
        return f"cd {shlex.quote(wd)} && {cmd}"
    return cmd


def common_compose_dir_patterns(project: str) -> list[str]:
    """Guest directories to probe when the working_dir label is missing."""
    return [
        f"/opt/{project}",
        f"/home/*/docker/{project}",
        f"/root/docker/{project}",
    ]


def cmd_find_common_compose_files(project: str) -> str:
    """List readable compose files under common homelab paths (guest shell)."""
    dirs = " ".join(common_compose_dir_patterns(project))
    names = " ".join(COMPOSE_BASENAMES)
    return (
        f"for d in {dirs}; do "
        '[ -d "$d" ] || continue; '
        f"for f in {names}; do "
        'p="$d/$f"; '
        'if [ -f "$p" ] && [ -r "$p" ]; then printf \'%s\\n\' "$p"; fi; '
        "done; "
        "done"
    )


def cmd_compose_ls_json() -> str:
    return "docker compose ls --all --format json"


def parse_compose_ls_json(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text or "")
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def compose_ls_match(
    entries: list[dict[str, Any]], project: str
) -> tuple[str | None, list[str]]:
    """Return (working_dir, config_files) for ``project`` from ``compose ls``."""
    want = (project or "").strip()
    for entry in entries:
        name = str(entry.get("Name") or entry.get("name") or "").strip()
        if name != want:
            continue
        raw = entry.get("ConfigFiles") or entry.get("config_files") or ""
        files = parse_compose_config_files(str(raw))
        abs_files = [p for p in (normalize_guest_path(f) for f in files) if p]
        working_dir = None
        if abs_files:
            working_dir = abs_files[0].rsplit("/", 1)[0] or None
        return working_dir, files
    return None, []


def compose_skip_reason_de() -> str:
    return _COMPOSE_SKIP_DE


def inspect_container_name(info: dict[str, Any]) -> str:
    return str(info.get("Name") or "").strip().lstrip("/")


def inspect_container_image(info: dict[str, Any]) -> str:
    config = info.get("Config") if isinstance(info.get("Config"), dict) else {}
    return str(config.get("Image") or "").strip()


def _mount_flag(mount: dict[str, Any]) -> list[str]:
    dest = str(mount.get("Destination") or mount.get("Target") or "").strip()
    if not dest:
        return []
    mtype = str(mount.get("Type") or "").lower()
    ro = bool(mount.get("RW") is False)
    suffix = ":ro" if ro else ""
    if mtype == "bind":
        source = str(mount.get("Source") or "").strip()
        if not source:
            return []
        return ["-v", f"{source}:{dest}{suffix}"]
    if mtype == "volume":
        name = str(mount.get("Name") or "").strip()
        if not name:
            return []
        return ["-v", f"{name}:{dest}{suffix}"]
    if mtype == "tmpfs":
        return ["--tmpfs", dest]
    return []


def _publish_flags(host_config: dict[str, Any]) -> list[str]:
    bindings = host_config.get("PortBindings") or {}
    if not isinstance(bindings, dict):
        return []
    flags: list[str] = []
    for container_port, hosts in bindings.items():
        if not isinstance(hosts, list):
            continue
        for bind in hosts:
            if not isinstance(bind, dict):
                continue
            host_ip = str(bind.get("HostIp") or "").strip()
            host_port = str(bind.get("HostPort") or "").strip()
            if not host_port:
                continue
            left = f"{host_ip}:{host_port}" if host_ip else host_port
            flags.extend(["-p", f"{left}:{container_port}"])
    return flags


def docker_create_argv_from_inspect(info: dict[str, Any]) -> list[str]:
    """Rebuild ``docker create`` from inspect so a pulled image can replace the container."""
    name = inspect_container_name(info)
    image = inspect_container_image(info)
    config = info.get("Config") if isinstance(info.get("Config"), dict) else {}
    host = info.get("HostConfig") if isinstance(info.get("HostConfig"), dict) else {}
    argv = ["docker", "create", "--name", name]
    restart = (host.get("RestartPolicy") or {}).get("Name") or ""
    if restart and restart != "no":
        retries = (host.get("RestartPolicy") or {}).get("MaximumRetryCount")
        if restart == "on-failure" and retries:
            argv.extend(["--restart", f"on-failure:{int(retries)}"])
        else:
            argv.extend(["--restart", str(restart)])
    mode = str(host.get("NetworkMode") or "").strip()
    if mode and mode not in ("default", "bridge"):
        argv.extend(["--network", mode])
    if host.get("Privileged"):
        argv.append("--privileged")
    user = str(config.get("User") or "").strip()
    if user:
        argv.extend(["--user", user])
    hostname = str(config.get("Hostname") or "").strip()
    if hostname:
        argv.extend(["--hostname", hostname])
    workdir = str(config.get("WorkingDir") or "").strip()
    if workdir:
        argv.extend(["--workdir", workdir])
    for env in config.get("Env") or []:
        if env:
            argv.extend(["-e", str(env)])
    labels = config.get("Labels") or {}
    if isinstance(labels, dict):
        for key, value in labels.items():
            if key:
                argv.extend(["--label", f"{key}={value}"])
    for mount in info.get("Mounts") or []:
        if isinstance(mount, dict):
            argv.extend(_mount_flag(mount))
    argv.extend(_publish_flags(host))
    argv.append(image)
    cmd = config.get("Cmd")
    if isinstance(cmd, list):
        argv.extend(str(x) for x in cmd)
    return argv


def extra_networks_from_inspect(info: dict[str, Any]) -> list[str]:
    """Networks to ``docker network connect`` after create (primary already on create)."""
    host = info.get("HostConfig") if isinstance(info.get("HostConfig"), dict) else {}
    primary = str(host.get("NetworkMode") or "").strip()
    settings = info.get("NetworkSettings") if isinstance(info.get("NetworkSettings"), dict) else {}
    networks = settings.get("Networks") or {}
    if not isinstance(networks, dict):
        return []
    extra: list[str] = []
    for name in networks:
        if not name or name == primary or name == "bridge":
            continue
        extra.append(str(name))
    return extra
