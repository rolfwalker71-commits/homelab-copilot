"""Build a nested Node → Guest → Docker view for the dashboard."""

from __future__ import annotations

import re
from typing import Any

from app.core.models import EntityStatus, TopologyEntity, TopologySnapshot

_REPLICA_SUFFIX = re.compile(r"^(.+)-(\d+)$")
_COMPOSE_PROJECT = "com.docker.compose.project"
_COMPOSE_SERVICE = "com.docker.compose.service"


def short_image(image: str | None, version: str | None = None) -> str | None:
    """Return a dense image label: ``repo:tag`` without registry path."""
    if image:
        leaf = image.rsplit("/", 1)[-1]
        if leaf:
            return leaf
    if version:
        return f":{version}"
    return None


def _compose_label(c: TopologyEntity, key: str) -> str | None:
    labels = c.labels or {}
    raw = labels.get(key)
    if raw is None and c.meta:
        # SSH discovery may stash compose fields in meta as well
        alt = {
            _COMPOSE_PROJECT: "compose_project",
            _COMPOSE_SERVICE: "compose_service",
        }.get(key)
        if alt:
            raw = c.meta.get(alt)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _name_prefix_stack(name: str) -> str | None:
    """Compose-style prefix: ``wallstreet-frontend-1`` → ``wallstreet``."""
    m = _REPLICA_SUFFIX.match(name)
    if not m:
        return None
    prefix = m.group(1)  # wallstreet-frontend
    if "-" not in prefix:
        return None
    return prefix.split("-", 1)[0]


def _enrich_container(c: TopologyEntity) -> dict[str, Any]:
    compose_project = _compose_label(c, _COMPOSE_PROJECT)
    compose_service = _compose_label(c, _COMPOSE_SERVICE)
    return {
        "entity": c,
        "name": c.name,
        "status": c.status.value,
        "image_short": short_image(c.image, c.version),
        "parent_id": c.parent_id,
        "compose_project": compose_project,
        "compose_service": compose_service,
    }


def group_docker_items(containers: list[TopologyEntity]) -> list[dict[str, Any]]:
    """Group Docker Compose projects first, then name-prefix stacks.

    - Prefer ``com.docker.compose.project`` (any size, including 1).
    - Fallback: shared name prefix before service/replica (needs ≥2 members).
    - Leftovers stay as flat ``container`` items (UI: Einzelcontainer).
    """
    enriched = [_enrich_container(c) for c in containers]

    by_compose: dict[str, list[dict[str, Any]]] = {}
    remainder: list[dict[str, Any]] = []

    for item in enriched:
        proj = item.get("compose_project")
        if proj:
            by_compose.setdefault(proj, []).append(item)
        else:
            remainder.append(item)

    items: list[dict[str, Any]] = []
    for key in sorted(by_compose.keys(), key=str.lower):
        group = sorted(by_compose[key], key=lambda x: x["name"].lower())
        items.append(
            {
                "kind": "stack",
                "name": key,
                "count": len(group),
                "source": "compose",
                "compose_project": key,
                "containers": group,
            }
        )

    by_prefix: dict[str, list[dict[str, Any]]] = {}
    singles: list[dict[str, Any]] = []
    for item in remainder:
        key = _name_prefix_stack(item["name"])
        if key:
            by_prefix.setdefault(key, []).append(item)
        else:
            singles.append(item)

    for key in sorted(by_prefix.keys(), key=str.lower):
        group = sorted(by_prefix[key], key=lambda x: x["name"].lower())
        if len(group) >= 2:
            items.append(
                {
                    "kind": "stack",
                    "name": key,
                    "count": len(group),
                    "source": "prefix",
                    "compose_project": None,
                    "containers": group,
                }
            )
        else:
            singles.extend(group)

    for item in sorted(singles, key=lambda x: x["name"].lower()):
        items.append({"kind": "container", **item})

    stacks = [i for i in items if i["kind"] == "stack"]
    flats = [i for i in items if i["kind"] == "container"]
    stacks.sort(key=lambda x: x["name"].lower())
    flats.sort(key=lambda x: x["name"].lower())
    return stacks + flats


def build_topology_tree(snapshot: TopologySnapshot | None) -> dict[str, Any]:
    """Group guests by Proxmox node and nest Docker under matching parent guests.

    Matching rule: ``container.parent_id == guest.id``.
    Orphans (local socket, missing parent, no parent_id) go under ``orphans``.
    """
    if snapshot is None:
        return {
            "nodes": [],
            "orphans": [],
            "orphan_items": [],
            "orphan_count": 0,
            "has_anything": False,
        }

    guest_ids = {g.id for g in snapshot.guests}
    by_parent: dict[str, list[TopologyEntity]] = {}
    orphans: list[TopologyEntity] = []

    for c in snapshot.containers:
        pid = c.parent_id
        if pid and pid in guest_ids:
            by_parent.setdefault(pid, []).append(c)
        else:
            orphans.append(c)

    nodes_by_name: dict[str, TopologyEntity] = {
        n.name: n for n in snapshot.nodes if n.name
    }

    guests_by_node: dict[str, list[TopologyEntity]] = {}
    for g in snapshot.guests:
        key = g.node or "unbekannt"
        guests_by_node.setdefault(key, []).append(g)

    # Stable order: known nodes first (as discovered), then leftover guest nodes
    ordered_names: list[str] = []
    for n in snapshot.nodes:
        if n.name and n.name not in ordered_names:
            ordered_names.append(n.name)
    for name in sorted(guests_by_node.keys()):
        if name not in ordered_names:
            ordered_names.append(name)

    nodes_out: list[dict[str, Any]] = []
    for name in ordered_names:
        guests = guests_by_node.get(name, [])
        guest_rows: list[dict[str, Any]] = []
        node_docker = 0
        for g in sorted(guests, key=lambda x: (x.vmid or 0, x.name.lower())):
            children = by_parent.get(g.id, [])
            children = sorted(children, key=lambda x: x.name.lower())
            node_docker += len(children)
            docker_items = group_docker_items(children)
            guest_rows.append(
                {
                    "guest": g,
                    "containers": children,
                    "docker_items": docker_items,
                    "docker_stacks": [i for i in docker_items if i["kind"] == "stack"],
                    "docker_singles": [i for i in docker_items if i["kind"] == "container"],
                    "docker_count": len(children),
                }
            )
        node_ent = nodes_by_name.get(name)
        running = sum(1 for g in guests if g.status == EntityStatus.RUNNING)
        stopped = sum(1 for g in guests if g.status == EntityStatus.STOPPED)
        meta = dict(node_ent.meta) if node_ent and node_ent.meta else {}
        nodes_out.append(
            {
                "name": name,
                "node": node_ent,
                "status": node_ent.status.value if node_ent else "unknown",
                "meta": meta,
                "guests": guest_rows,
                "guest_count": len(guest_rows),
                "guest_running": running,
                "guest_stopped": stopped,
                "docker_count": node_docker,
            }
        )

    orphans_sorted = sorted(orphans, key=lambda x: x.name.lower())
    orphan_items = group_docker_items(orphans_sorted)
    return {
        "nodes": nodes_out,
        "orphans": orphans_sorted,
        "orphan_items": orphan_items,
        "orphan_stacks": [i for i in orphan_items if i["kind"] == "stack"],
        "orphan_singles": [i for i in orphan_items if i["kind"] == "container"],
        "orphan_count": len(orphans_sorted),
        "has_anything": bool(nodes_out or orphans_sorted),
    }
