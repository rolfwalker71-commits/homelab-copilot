"""Preflight inventory for a Compose stack via docker inspect."""

from __future__ import annotations

import json
import logging
import re
import shlex
from typing import Any

from app.config import Settings
from app.core.docker_control import (
    DockerControlError,
    resolve_parent_ip,
    ssh_key_present,
    validate_docker_name,
)
from app.core.models import TopologySnapshot

from backup_verifier import sshutil

logger = logging.getLogger(__name__)

_SAFE_PATH = re.compile(r"^[\w./@+=:,-]+$")

# Host paths that look like binds but are not useful backup targets
_SKIP_BIND_PREFIXES = (
    "/proc",
    "/sys",
    "/dev",
    "/run/",
    "/var/run/",
)
_SKIP_BIND_EXACT = {
    "/var/run/docker.sock",
    "/run/docker.sock",
}

# image substring → engine label (order matters: more specific first)
_DB_IMAGE_HINTS: tuple[tuple[str, str], ...] = (
    ("postgres", "PostgreSQL"),
    ("postgis", "PostgreSQL"),
    ("timescale", "PostgreSQL"),
    ("mariadb", "MariaDB"),
    ("mysql", "MySQL"),
    ("percona", "MySQL"),
    ("mongo", "MongoDB"),
    ("redis", "Redis"),
    ("keydb", "Redis"),
    ("valkey", "Redis"),
)


def _skip_bind_reason(source: str) -> str | None:
    """Return skip reason if bind should not be backed up, else None."""
    if not source:
        return "Leerer Host-Pfad"
    if source in _SKIP_BIND_EXACT:
        return "Docker-Socket / Runtime — kein sinnvolles Backup-Ziel"
    for p in _SKIP_BIND_PREFIXES:
        if source.startswith(p):
            return f"Spezial-Pfad ({p.rstrip('/')}) — übersprungen"
    return None


def _detect_db_engine(image: str) -> str | None:
    if not image:
        return None
    low = image.lower()
    # strip registry/tag noise for matching
    for needle, label in _DB_IMAGE_HINTS:
        if needle in low:
            return label
    return None


def _container_image(info: dict[str, Any]) -> str:
    cfg = info.get("Config") or {}
    image = cfg.get("Image") or ""
    if not image:
        image = (info.get("Image") or "")[:12]
    return str(image)


def _guest_name(snapshot: TopologySnapshot | None, parent_id: str) -> str:
    if snapshot is None:
        return parent_id
    for g in snapshot.guests:
        if g.id == parent_id:
            return g.name
    if parent_id == "local:docker":
        return "local-docker"
    return parent_id


async def build_inventory(
    settings: Settings,
    *,
    parent_id: str,
    project: str,
    snapshot: TopologySnapshot | None,
    lxc_backup_dir: str,
) -> dict[str, Any]:
    """Return preflight inventory: what will be backed up and known gaps."""
    project = validate_docker_name(project, kind="Compose-Projekt")
    guest = _guest_name(snapshot, parent_id)
    ip = resolve_parent_ip(snapshot, parent_id)
    local = ip is None

    if not local and not ssh_key_present(settings):
        raise DockerControlError(
            "SSH-Schlüssel fehlt — Backup auf Remotes nicht möglich.",
            status_code=503,
        )

    topo_containers = _containers_for_project(snapshot, parent_id, project)
    warnings: list[str] = [
        "Backup-Einheit = gesamter Compose-Stack (nicht einzelner Container).",
        "Bind-Mounts nur, wenn der Host-Pfad auf dem LXC lesbar ist.",
        "Kein Ersatz für Proxmox vzdump (vollständige LXC-DR).",
    ]
    gaps: list[str] = []
    scope_notes: list[str] = []

    if local:
        inspects = await _local_inspect_project(settings, project)
    else:
        assert ip is not None
        inspects = await _remote_inspect_project(settings, ip, project)

    if not inspects and not topo_containers:
        raise DockerControlError(
            f"Keine Container für Compose-Projekt „{project}“ gefunden.",
            status_code=404,
        )

    working_dir: str | None = None
    config_files: list[str] = []
    named_volumes: dict[str, dict[str, Any]] = {}
    bind_mounts: dict[str, dict[str, Any]] = {}
    bind_skipped: list[dict[str, Any]] = []
    anon_count = 0
    services: list[dict[str, Any]] = []
    databases: list[dict[str, Any]] = []

    for info in inspects:
        name = (info.get("Name") or "").lstrip("/")
        image = _container_image(info)
        state = ((info.get("State") or {}).get("Status") or "").lower()
        labels = (info.get("Config") or {}).get("Labels") or {}
        service = labels.get("com.docker.compose.service") or name

        db_engine = _detect_db_engine(image)
        svc: dict[str, Any] = {
            "name": name or service,
            "service": service,
            "image": image or "—",
            "status": state or "unknown",
            "db_engine": db_engine,
            "quiesce_recommended": bool(db_engine),
        }
        services.append(svc)
        if db_engine:
            databases.append(
                {
                    "container": name or service,
                    "service": service,
                    "image": image,
                    "engine": db_engine,
                    "quiesce": True,
                    "note": (
                        f"Heuristik über Image — bei Quiesce wird der Stack "
                        f"gestoppt (empfohlen für konsistente {db_engine}-Daten)."
                    ),
                }
            )

        wd = labels.get("com.docker.compose.project.working_dir")
        if wd and not working_dir:
            working_dir = wd
        cf = labels.get("com.docker.compose.project.config_files")
        if cf:
            for part in re.split(r"[,:]", cf):
                part = part.strip()
                if part and part not in config_files:
                    config_files.append(part)

        for m in info.get("Mounts") or []:
            mtype = (m.get("Type") or "").lower()
            source = m.get("Source") or ""
            dest = m.get("Destination") or m.get("Target") or ""
            vol_name = m.get("Name") or ""
            if mtype == "volume":
                if vol_name and not vol_name.startswith((".",)):
                    # Named volume (has a real name); anonymous often have hash names
                    if re.fullmatch(r"[0-9a-f]{64}", vol_name):
                        anon_count += 1
                    else:
                        named_volumes[vol_name] = {
                            "name": vol_name,
                            "destination": dest,
                            "source": source,
                            "rw": bool(m.get("RW", True)),
                            "will_backup": True,
                            "note": "Named Volume — wird per Helper-Container getar't",
                        }
                else:
                    anon_count += 1
            elif mtype == "bind":
                skip_reason = _skip_bind_reason(source)
                if skip_reason:
                    bind_skipped.append(
                        {
                            "source": source,
                            "destination": dest,
                            "included": False,
                            "will_backup": False,
                            "reason": skip_reason,
                        }
                    )
                elif source:
                    bind_mounts[source] = {
                        "source": source,
                        "destination": dest,
                        "rw": bool(m.get("RW", True)),
                        "readable": None,
                        "included": None,
                        "will_backup": None,
                        "reason": "",
                    }

    # Fallback from topology if inspect returned nothing useful for names
    if not services and topo_containers:
        for c in topo_containers:
            image = c.get("image") or "—"
            db_engine = _detect_db_engine(image)
            services.append(
                {
                    "name": c["name"],
                    "service": c["name"],
                    "image": image,
                    "status": c.get("status") or "unknown",
                    "db_engine": db_engine,
                    "quiesce_recommended": bool(db_engine),
                }
            )
            if db_engine:
                databases.append(
                    {
                        "container": c["name"],
                        "service": c["name"],
                        "image": image,
                        "engine": db_engine,
                        "quiesce": True,
                        "note": (
                            f"Heuristik über Image — Quiesce empfohlen "
                            f"für {db_engine}."
                        ),
                    }
                )

    if anon_count:
        note = (
            f"{anon_count} anonyme Volume(s): Daten liegen nur in der "
            "Container-Schicht und werden nicht separat gesichert."
        )
        gaps.append(note)
        scope_notes.append(note)
    gaps.append(
        "Externe NFS/CIFS-Shares nur, wenn vom LXC lesbar; "
        "Swarm/Kubernetes nicht unterstützt."
    )
    gaps.append(
        "Daten außerhalb der per docker inspect sichtbaren Mounts "
        "werden nicht erfasst."
    )

    # Probe bind readability
    for src, meta in bind_mounts.items():
        readable = await _path_readable(settings, ip, src, local=local)
        meta["readable"] = readable
        if readable:
            meta["included"] = True
            meta["will_backup"] = True
            meta["reason"] = "Host-Pfad lesbar — wird mitgesichert"
        else:
            meta["included"] = False
            meta["will_backup"] = False
            meta["reason"] = "Host-Pfad nicht lesbar auf dem Backup-Host"
            gaps.append(f"Bind-Mount nicht lesbar: {src}")
            scope_notes.append(f"Unlesbarer Bind-Mount: {src}")

    for b in bind_skipped:
        gaps.append(f"Spezial-Bind übersprungen: {b['source']}")

    compose_candidates = list(config_files)
    if working_dir:
        for name in (
            "docker-compose.yml",
            "docker-compose.yaml",
            "compose.yml",
            "compose.yaml",
        ):
            p = f"{working_dir.rstrip('/')}/{name}"
            if p not in compose_candidates:
                compose_candidates.append(p)
        env_path = f"{working_dir.rstrip('/')}/.env"
    else:
        env_path = None
        warnings.append(
            "Kein Compose working_dir in Labels — Compose-Dateien ggf. unvollständig."
        )

    config_entries: list[dict[str, Any]] = []
    existing_compose: list[str] = []
    missing_compose: list[str] = []
    seen_paths: set[str] = set()
    label_paths = set(config_files)
    guessed = [p for p in compose_candidates if p not in label_paths]

    # Probe label-declared compose files first
    for p in compose_candidates:
        if p in seen_paths or p not in label_paths:
            continue
        seen_paths.add(p)
        exists = await _path_readable(settings, ip, p, local=local)
        config_entries.append(
            {
                "path": p,
                "kind": "compose",
                "exists": exists,
                "missing": not exists,
                "will_backup": exists,
                "status": "gefunden" if exists else "fehlt",
                "source": "label",
            }
        )
        if exists:
            existing_compose.append(p)
        else:
            missing_compose.append(p)

    # Working-dir guesses: keep found files; if none found at all, list guesses as missing
    for p in guessed:
        if p in seen_paths:
            continue
        seen_paths.add(p)
        exists = await _path_readable(settings, ip, p, local=local)
        if exists:
            config_entries.append(
                {
                    "path": p,
                    "kind": "compose",
                    "exists": True,
                    "missing": False,
                    "will_backup": True,
                    "status": "gefunden",
                    "source": "working_dir",
                }
            )
            existing_compose.append(p)

    if not existing_compose and guessed:
        for p in guessed:
            if any(e["path"] == p for e in config_entries):
                continue
            config_entries.append(
                {
                    "path": p,
                    "kind": "compose",
                    "exists": False,
                    "missing": True,
                    "will_backup": False,
                    "status": "fehlt",
                    "source": "working_dir",
                }
            )
            missing_compose.append(p)
        scope_notes.append(
            "Keine Compose-Datei am erwarteten Pfad lesbar — "
            "Config ggf. unvollständig."
        )

    env_exists = False
    env_entry: dict[str, Any] | None = None
    if env_path:
        env_exists = await _path_readable(settings, ip, env_path, local=local)
        env_entry = {
            "path": env_path,
            "kind": "env",
            "exists": env_exists,
            "missing": not env_exists,
            "will_backup": env_exists,
            "status": "gefunden" if env_exists else "fehlt",
        }
        config_entries.append(env_entry)
        if not env_exists:
            scope_notes.append(f".env erwartet, aber nicht lesbar: {env_path}")

    if databases:
        engines = sorted({d["engine"] for d in databases})
        warnings.append(
            "Datenbank-Images erkannt ("
            + ", ".join(engines)
            + ") — Quiesce empfohlen für konsistente Snapshots."
        )

    all_binds = list(bind_mounts.values()) + bind_skipped
    readable_binds = [b for b in bind_mounts.values() if b.get("will_backup")]
    skipped_binds = [
        b for b in all_binds if not b.get("will_backup")
    ]

    inventory: dict[str, Any] = {
        "stack": project,
        "project": project,
        "compose_project": project,
        "parent_id": parent_id,
        "guest_name": guest,
        "host_ip": ip,
        "local": local,
        "working_dir": working_dir,
        # Config
        "config_files": config_entries,
        "compose_files": existing_compose,
        "compose_files_missing": missing_compose,
        "env_file": env_path if env_exists else None,
        "env_expected": env_path,
        "env_missing": bool(env_path and not env_exists),
        # Volumes & mounts
        "named_volumes": list(named_volumes.values()),
        "bind_mounts": list(bind_mounts.values()),
        "bind_mounts_all": all_binds,
        "bind_mounts_skipped": bind_skipped + [
            b for b in bind_mounts.values() if not b.get("will_backup")
        ],
        "anonymous_volume_count": anon_count,
        # Services
        "services": services,
        "containers": [s["name"] for s in services],
        "container_count": len(services),
        "databases": databases,
        "quiesce_recommended": bool(databases),
        # Meta
        "lxc_backup_dir": lxc_backup_dir,
        "warnings": warnings,
        "gaps": gaps,
        "scope_notes": scope_notes,
        "include_summary": {
            "compose_files": len(existing_compose),
            "compose_missing": len(missing_compose),
            "env": bool(env_exists),
            "named_volumes": len(named_volumes),
            "bind_mounts_readable": len(readable_binds),
            "bind_mounts_skipped": len(skipped_binds),
            "services": len(services),
            "databases": len(databases),
            "anonymous_volumes": anon_count,
        },
    }
    return inventory


def _containers_for_project(
    snapshot: TopologySnapshot | None, parent_id: str, project: str
) -> list[dict[str, Any]]:
    if snapshot is None:
        return []
    out = []
    for c in snapshot.containers:
        if c.parent_id != parent_id:
            continue
        labels = c.labels or {}
        proj = labels.get("com.docker.compose.project") or (c.meta or {}).get(
            "compose_project"
        )
        if proj == project:
            out.append(
                {
                    "name": c.name,
                    "status": c.status.value if c.status else "",
                    "image": c.image or "",
                }
            )
    return out


async def _remote_inspect_project(
    settings: Settings, ip: str, project: str
) -> list[dict[str, Any]]:
    list_cmd = (
        "docker ps -aq --filter "
        f"label=com.docker.compose.project={shlex.quote(project)}"
    )
    stdout, _, code = await sshutil.ssh_run(settings, ip, list_cmd, timeout=30)
    if code != 0:
        return []
    ids = [x.strip() for x in stdout.splitlines() if x.strip()]
    if not ids:
        return []
    # Limit inspect batch size
    ids = ids[:50]
    id_args = " ".join(shlex.quote(i) for i in ids)
    out = await sshutil.ssh_run_ok(
        settings, ip, f"docker inspect {id_args}", timeout=60
    )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise DockerControlError(
            f"docker inspect JSON ungültig: {exc}", status_code=502
        ) from exc
    return data if isinstance(data, list) else []


async def _local_inspect_project(
    settings: Settings, project: str
) -> list[dict[str, Any]]:
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

    import asyncio

    return await asyncio.to_thread(_sync)


async def _path_readable(
    settings: Settings,
    ip: str | None,
    path: str,
    *,
    local: bool,
) -> bool:
    if not path or path.startswith("/proc") or path.startswith("/sys"):
        return False
    check = f"test -r {shlex.quote(path)} && test -e {shlex.quote(path)}"
    if local:
        stdout, _, code = await sshutil.local_run(check, timeout=10)
        return code == 0
    assert ip is not None
    _, _, code = await sshutil.ssh_run(settings, ip, check, timeout=15)
    return code == 0
