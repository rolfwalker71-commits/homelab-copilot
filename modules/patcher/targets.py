"""Merge Proxmox guests + manual hosts into patch targets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import Settings, get_settings
from app.core.docker_control import DockerControlError
from app.core.models import EntityKind, EntityStatus, TopologySnapshot

from patcher.store import PatcherStore


@dataclass
class PatchTarget:
    id: str
    name: str
    kind: str  # lxc | qemu | manual
    ip: str
    port: int
    ssh_user: str | None
    node: str | None = None
    vmid: int | None = None
    hostname: str | None = None
    note: str = ""
    source: str = "topology"  # topology | manual

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "ip": self.ip,
            "port": self.port,
            "ssh_user": self.ssh_user,
            "node": self.node,
            "vmid": self.vmid,
            "hostname": self.hostname,
            "note": self.note,
            "source": self.source,
        }


def guests_from_snapshot(
    snapshot: TopologySnapshot | None,
    *,
    settings: Settings | None = None,
) -> list[PatchTarget]:
    settings = settings or get_settings()
    if snapshot is None:
        return []
    out: list[PatchTarget] = []
    for g in snapshot.guests:
        kind = g.kind.value if hasattr(g.kind, "value") else str(g.kind)
        if kind not in (EntityKind.LXC.value, EntityKind.QEMU.value):
            continue
        status = g.status.value if hasattr(g.status, "value") else str(g.status)
        if status != EntityStatus.RUNNING.value:
            continue
        if not g.ip_addresses:
            continue
        out.append(
            PatchTarget(
                id=g.id,
                name=g.name,
                kind=kind,
                ip=g.ip_addresses[0],
                port=settings.docker_ssh_port,
                ssh_user=None,
                node=g.node,
                vmid=g.vmid,
                hostname=g.hostname,
                source="topology",
            )
        )
    out.sort(key=lambda t: t.name.lower())
    return out


async def manual_targets(
    store: PatcherStore,
    *,
    settings: Settings | None = None,
) -> list[PatchTarget]:
    settings = settings or get_settings()
    rows = await store.list_hosts(enabled_only=True)
    out: list[PatchTarget] = []
    for h in rows:
        out.append(
            PatchTarget(
                id=f"manual:{h['id']}",
                name=h["name"],
                kind="manual",
                ip=h["host"],
                port=int(h.get("port") or settings.docker_ssh_port),
                ssh_user=(h.get("ssh_user") or None),
                note=h.get("note") or "",
                source="manual",
            )
        )
    return out


async def list_targets(
    store: PatcherStore,
    snapshot: TopologySnapshot | None,
    *,
    settings: Settings | None = None,
) -> list[PatchTarget]:
    settings = settings or get_settings()
    guests = guests_from_snapshot(snapshot, settings=settings)
    manuals = await manual_targets(store, settings=settings)
    return guests + manuals


async def resolve_target(
    store: PatcherStore,
    snapshot: TopologySnapshot | None,
    target_id: str,
    *,
    settings: Settings | None = None,
) -> PatchTarget:
    settings = settings or get_settings()
    target_id = (target_id or "").strip()
    if not target_id:
        raise DockerControlError("target_id fehlt.", status_code=400)

    if target_id.startswith("manual:"):
        try:
            hid = int(target_id.split(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise DockerControlError(
                f"Ungültige Manual-Host-ID: {target_id}",
                status_code=400,
            ) from exc
        row = await store.get_host(hid)
        if not row:
            raise DockerControlError(
                f"Manueller Host {hid} nicht gefunden.",
                status_code=404,
            )
        if not row.get("enabled", True):
            raise DockerControlError(
                f"Manueller Host „{row.get('name')}“ ist deaktiviert.",
                status_code=400,
            )
        return PatchTarget(
            id=target_id,
            name=row["name"],
            kind="manual",
            ip=row["host"],
            port=int(row.get("port") or settings.docker_ssh_port),
            ssh_user=(row.get("ssh_user") or None),
            note=row.get("note") or "",
            source="manual",
        )

    if snapshot is None:
        raise DockerControlError(
            "Keine Topologie geladen — bitte zuerst Discovery ausführen.",
            status_code=404,
        )
    for g in snapshot.guests:
        if g.id != target_id:
            continue
        kind = g.kind.value if hasattr(g.kind, "value") else str(g.kind)
        if kind not in (EntityKind.LXC.value, EntityKind.QEMU.value):
            raise DockerControlError(
                f"Ziel „{g.name}“ ist kein LXC/QEMU-Guest.",
                status_code=400,
            )
        status = g.status.value if hasattr(g.status, "value") else str(g.status)
        if status != EntityStatus.RUNNING.value:
            raise DockerControlError(
                f"Guest „{g.name}“ läuft nicht (Status: {status}).",
                status_code=400,
            )
        if not g.ip_addresses:
            raise DockerControlError(
                f"Guest „{g.name}“ hat keine IP — SSH nicht möglich.",
                status_code=400,
            )
        return PatchTarget(
            id=g.id,
            name=g.name,
            kind=kind,
            ip=g.ip_addresses[0],
            port=settings.docker_ssh_port,
            ssh_user=None,
            node=g.node,
            vmid=g.vmid,
            hostname=g.hostname,
            source="topology",
        )

    raise DockerControlError(
        f"Ziel „{target_id}“ nicht in der Topologie gefunden.",
        status_code=404,
    )
