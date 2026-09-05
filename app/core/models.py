"""Domain models for the unified infrastructure topology."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EntityKind(str, Enum):
    NODE = "node"
    LXC = "lxc"
    QEMU = "qemu"
    DOCKER = "docker"
    HOST = "host"


class EntityStatus(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    UNKNOWN = "unknown"
    PAUSED = "paused"
    ERROR = "error"


class TopologyEntity(BaseModel):
    """Single discovered infrastructure entity."""

    id: str
    kind: EntityKind
    name: str
    status: EntityStatus = EntityStatus.UNKNOWN
    node: str | None = None
    vmid: int | None = None
    hostname: str | None = None
    ip_addresses: list[str] = Field(default_factory=list)
    parent_id: str | None = None
    image: str | None = None
    version: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    discovered_at: str | None = None  # German formatted
    discovered_at_iso: str | None = None


class TopologySnapshot(BaseModel):
    """Full topology tree at a point in time."""

    refreshed_at: str
    refreshed_at_iso: str
    source: str = "discovery"
    nodes: list[TopologyEntity] = Field(default_factory=list)
    guests: list[TopologyEntity] = Field(default_factory=list)  # LXC + QEMU
    hosts: list[TopologyEntity] = Field(default_factory=list)  # manual Linux hosts
    containers: list[TopologyEntity] = Field(default_factory=list)  # Docker
    errors: list[str] = Field(default_factory=list)
    proxmox_configured: bool = False

    @property
    def summary(self) -> dict[str, int]:
        return {
            "nodes": len(self.nodes),
            "guests": len(self.guests),
            "hosts": len(self.hosts),
            "containers": len(self.containers),
            "errors": len(self.errors),
        }
