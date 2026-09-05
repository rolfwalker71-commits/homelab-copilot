"""Resolve SSH IP / port / user from a topology entity (guest, host, or node)."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings
from app.core.models import TopologyEntity, TopologySnapshot


@dataclass(frozen=True)
class SshEndpoint:
    ip: str
    port: int
    username: str
    entity: TopologyEntity


def find_topology_entity(
    snapshot: TopologySnapshot | None, entity_id: str
) -> TopologyEntity | None:
    """Find a guest, host, or node by id."""
    entity_id = (entity_id or "").strip()
    if not entity_id or snapshot is None:
        return None
    for g in snapshot.guests:
        if g.id == entity_id:
            return g
    for h in snapshot.hosts:
        if h.id == entity_id:
            return h
    for n in snapshot.nodes:
        if n.id == entity_id or (
            entity_id.startswith("node:") and n.name == entity_id.split(":", 1)[-1]
        ):
            return n
    return None


def endpoint_from_entity(
    entity: TopologyEntity, settings: Settings | None = None
) -> SshEndpoint | None:
    """Build an SSH endpoint from entity IPs + optional per-host meta overrides."""
    settings = settings or get_settings()
    if not entity.ip_addresses:
        return None
    meta = entity.meta or {}
    try:
        port = int(meta.get("ssh_port") or settings.docker_ssh_port)
    except (TypeError, ValueError):
        port = settings.docker_ssh_port
    user = str(meta.get("ssh_user") or "").strip() or settings.docker_ssh_user
    return SshEndpoint(
        ip=entity.ip_addresses[0],
        port=port,
        username=user,
        entity=entity,
    )


def resolve_ssh_endpoint(
    snapshot: TopologySnapshot | None,
    entity_id: str,
    settings: Settings | None = None,
) -> SshEndpoint | None:
    entity = find_topology_entity(snapshot, entity_id)
    if entity is None:
        return None
    return endpoint_from_entity(entity, settings)
