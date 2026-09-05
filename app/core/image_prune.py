"""Safe unused-image cleanup after a compose/stack upgrade (pure helpers).

Does not run Docker. Callers execute commands on the stack's Docker host
and only remove IDs this upgrade replaced, plus dangling ``<none>`` images.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

# Docker image IDs are hex; accept short (12) through full sha256 (64).
_IMAGE_ID_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{12,64})$", re.IGNORECASE)
_RECLAIMED_RE = re.compile(r"Total reclaimed space:\s*(.+)\s*$", re.IGNORECASE)


def normalize_image_id(value: str) -> str:
    """Return lowercase hex image id, or empty if not a safe Docker image id."""
    match = _IMAGE_ID_RE.match((value or "").strip())
    if not match:
        return ""
    return match.group(1).lower()


def parse_image_id_lines(text: str) -> set[str]:
    """Parse ``docker inspect --format '{{.Image}}'`` (one id per line)."""
    ids: set[str] = set()
    for line in (text or "").splitlines():
        nid = normalize_image_id(line)
        if nid:
            ids.add(nid)
    return ids


def replaced_image_ids_to_remove(
    *,
    previous_ids: set[str] | list[str],
    current_ids: set[str] | list[str],
    in_use_ids: set[str] | list[str],
) -> list[str]:
    """Image IDs this upgrade replaced that no container on the host still uses.

    ``in_use_ids`` must be host-wide (every container, running or stopped).
    Current stack images are treated as in-use even if omitted from that set.
    """

    def _norm(values: set[str] | list[str]) -> set[str]:
        return {normalize_image_id(v) for v in values if normalize_image_id(v)}

    previous = _norm(previous_ids)
    current = _norm(current_ids)
    in_use = _norm(in_use_ids)
    return sorted(previous - current - in_use)


def docker_rmi_ref(image_id: str) -> str | None:
    """``sha256:<hex>`` for ``docker rmi``, or None if the id is not safe."""
    nid = normalize_image_id(image_id)
    if not nid:
        return None
    return f"sha256:{nid}"


def parse_image_prune_output(text: str) -> dict[str, Any]:
    """Parse ``docker image prune -f`` (or ``docker rmi``) human output."""
    deleted = 0
    untagged = 0
    reclaimed = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        lower = line.lower()
        if lower.startswith("deleted:"):
            deleted += 1
        elif lower.startswith("untagged:"):
            untagged += 1
        else:
            match = _RECLAIMED_RE.search(line)
            if match:
                reclaimed = match.group(1).strip()
    return {
        "deleted": deleted,
        "untagged": untagged,
        "reclaimed": reclaimed,
    }


def format_unused_image_cleanup_message(
    *,
    dangling_deleted: int = 0,
    dangling_untagged: int = 0,
    replaced_removed: int = 0,
    reclaimed: str = "",
    warning: str | None = None,
) -> str:
    """German job/toast line for cleanup. Warning means upgrade still succeeded."""
    if warning:
        detail = (warning or "").strip()[:180] or "unbekannter Fehler"
        return (
            f"Warnung: Image-Bereinigung fehlgeschlagen ({detail}). "
            "Das Upgrade war erfolgreich; die Bereinigung ist optional."
        )
    total = max(0, int(dangling_deleted)) + max(0, int(replaced_removed))
    tags = max(0, int(dangling_untagged))
    space = (reclaimed or "").strip()
    space_note = ""
    if space and space.upper() not in {"0B", "0"}:
        space_note = f", {space} freigegeben"
    if total == 0 and tags == 0:
        return "Keine ungenutzten Images zum Entfernen."
    if total:
        return f"Ungenutzte Images entfernt: {total} Image(s){space_note}."
    return f"Ungenutzte Image-Tags entfernt: {tags}{space_note}."


def cmd_dangling_image_prune() -> str:
    """Safe: only dangling ``<none>`` images, not ``--all`` / system prune."""
    return "docker image prune -f"


def cmd_project_image_ids(project: str) -> str:
    """Image IDs currently used by a compose project's containers."""
    return (
        "cids=$(docker ps -aq --filter "
        f"label=com.docker.compose.project={shlex.quote(project)}); "
        "if [ -n \"$cids\" ]; then docker inspect --format '{{.Image}}' $cids; fi"
    )


def cmd_named_image_ids(names: list[str]) -> str:
    quoted = " ".join(shlex.quote(n) for n in names if n)
    return "docker inspect --format '{{.Image}}' -- " + quoted


def cmd_in_use_image_ids() -> str:
    """Image IDs referenced by any container on this Docker host."""
    return (
        "cids=$(docker ps -aq); "
        "if [ -n \"$cids\" ]; then docker inspect --format '{{.Image}}' $cids; fi"
    )


def cmd_rmi_unused(image_ids: list[str]) -> str | None:
    """``docker rmi`` without ``-f`` so in-use images are refused."""
    refs = [docker_rmi_ref(i) for i in image_ids]
    safe = [r for r in refs if r]
    if not safe:
        return None
    return "docker rmi -- " + " ".join(shlex.quote(r) for r in safe)


def compose_projects_for_container_names(
    name_to_project: dict[str, str],
    names: list[str],
) -> tuple[list[str], list[str]]:
    """Split container names into compose projects vs. leftover (no project)."""
    projects: list[str] = []
    leftover: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = (name or "").strip()
        if not key:
            continue
        project = (name_to_project.get(key) or name_to_project.get(key.lstrip("/")) or "").strip()
        if project:
            if project not in seen:
                seen.add(project)
                projects.append(project)
        else:
            leftover.append(key)
    return projects, leftover
