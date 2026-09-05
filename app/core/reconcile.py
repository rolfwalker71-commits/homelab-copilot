"""Reconcile a live Proxmox discovery against the cached topology.

Live qemu/lxc guests with a real status (and config on owned nodes) are the
source of truth for the Hosts rail. Vanished VMIDs and unknown leftovers
(``lxc-114`` without config) are dropped; a recreate that keeps the name
(stirlingpdf 104→108) updates vmid/node/status/ip. Manual Linux hosts are
never deleted here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.models import EntityKind, TopologyEntity, TopologySnapshot

_PVE_KINDS = {EntityKind.LXC, EntityKind.QEMU}


@dataclass(frozen=True)
class ReconcileStats:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    id_changes: tuple[tuple[str, str], ...] = ()
    removed_ids: tuple[str, ...] = ()
    added_from: tuple[tuple[str, str], ...] = ()
    pve_kept_previous: bool = False

    def _added_debug_de(self) -> str:
        if not self.added:
            return ""
        if not self.added_from:
            return f"{self.added} neu"
        shown = self.added_from[:8]
        bits = ", ".join(f"{name}←{src}" for name, src in shown)
        extra = "…" if len(self.added_from) > 8 else ""
        return f"{self.added} neu ({bits}{extra})"

    def message_de(self) -> str:
        if self.pve_kept_previous:
            return "Proxmox-Discovery fehlgeschlagen — Hosts unverändert"
        parts = [
            f"{self.removed} Gäste entfernt",
            f"{self.updated} aktualisiert",
        ]
        added_text = self._added_debug_de()
        if added_text:
            parts.append(added_text)
        text = ", ".join(parts)
        if self.removed == 0 and self.updated == 0 and self.added == 0:
            return f"{text} — Topologie entspricht Proxmox"
        return text

    def as_dict(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "updated": self.updated,
            "removed": self.removed,
            "unchanged": self.unchanged,
            "id_changes": [list(pair) for pair in self.id_changes],
            "removed_ids": list(self.removed_ids),
            "added_from": [list(pair) for pair in self.added_from],
            "pve_kept_previous": self.pve_kept_previous,
            "message": self.message_de(),
        }


def guests_on_listed_nodes(
    guests: list[TopologyEntity],
    listed_nodes: set[str],
) -> list[TopologyEntity]:
    """Drop guests whose node is not in the live ``/nodes`` list."""
    if not listed_nodes:
        return list(guests)
    out: list[TopologyEntity] = []
    for g in guests:
        node = (g.node or "").strip()
        if not node or node in listed_nodes:
            out.append(g)
    return out


def _kind(ent: TopologyEntity) -> EntityKind:
    raw = ent.kind
    if isinstance(raw, EntityKind):
        return raw
    return EntityKind(str(raw))


def _is_pve_guest(ent: TopologyEntity) -> bool:
    return _kind(ent) in _PVE_KINDS


def guest_source_label(ent: TopologyEntity) -> str:
    """Toast origin: node name (pve01/pve02) or inventory/manual."""
    meta = ent.meta or {}
    src = str(meta.get("pve_source") or "").strip()
    if src:
        return src
    node = (ent.node or "").strip()
    if node:
        return node
    if str(meta.get("source") or "") == "manual":
        return "inventory"
    return "inventory"


def is_live_rail_guest(ent: TopologyEntity) -> bool:
    """True for qemu/lxc the Hosts rail may show (not template / unknown)."""
    if not _is_pve_guest(ent):
        return False
    if (ent.meta or {}).get("template"):
        return False
    return _status_value(ent) in {"running", "stopped", "paused"}


def _id_key(ent: TopologyEntity) -> tuple[str, str, int]:
    return (
        _kind(ent).value,
        (ent.node or "").strip().lower(),
        int(ent.vmid or 0),
    )


def _name_key(ent: TopologyEntity) -> tuple[str, str, str]:
    return (
        _kind(ent).value,
        (ent.node or "").strip().lower(),
        (ent.name or "").strip().lower(),
    )


def _is_fallback_name(ent: TopologyEntity) -> bool:
    kind = _kind(ent).value
    vmid = int(ent.vmid or 0)
    if not vmid:
        return False
    return (ent.name or "").strip().lower() == f"{kind}-{vmid}"


def _status_value(ent: TopologyEntity) -> str:
    st = ent.status
    return st.value if hasattr(st, "value") else str(st or "")


def _guest_fingerprint(ent: TopologyEntity) -> tuple[Any, ...]:
    return (
        ent.name,
        _status_value(ent),
        ent.node,
        int(ent.vmid or 0),
        tuple(ent.ip_addresses or []),
    )


def _pve_unavailable(live: TopologySnapshot) -> bool:
    """True when Proxmox was configured but the poll produced no live tree."""
    if not live.proxmox_configured:
        return False
    if live.nodes or live.guests:
        return False
    text = " ".join(live.errors or [])
    return "fehlgeschlagen" in text.lower()


def _manual_hosts_failed(live: TopologySnapshot) -> bool:
    return any("Manuelle Hosts" in (e or "") for e in (live.errors or []))


def _fill_ips_from_previous(
    live: TopologyEntity,
    previous: TopologyEntity | None,
) -> TopologyEntity:
    """Keep last-known IPs only for the same VMID when live has none."""
    if previous is None or live.ip_addresses:
        return live
    if int(live.vmid or 0) != int(previous.vmid or 0):
        return live
    if not previous.ip_addresses:
        return live
    return live.model_copy(update={"ip_addresses": list(previous.ip_addresses)})


def reconcile_topology(
    previous: TopologySnapshot | None,
    live: TopologySnapshot,
) -> tuple[TopologySnapshot, ReconcileStats]:
    """Replace PVE guests with the live list; keep manuals; report a German delta."""
    if previous is not None and _pve_unavailable(live):
        kept = previous.model_copy(
            update={
                "refreshed_at": live.refreshed_at,
                "refreshed_at_iso": live.refreshed_at_iso,
                "errors": list(live.errors),
                "proxmox_configured": live.proxmox_configured,
                "hosts": (
                    list(previous.hosts)
                    if _manual_hosts_failed(live)
                    else list(live.hosts)
                ),
                "containers": list(live.containers),
            }
        )
        return kept, ReconcileStats(pve_kept_previous=True)

    listed = {(n.name or "").strip() for n in live.nodes if (n.name or "").strip()}
    live_guests = guests_on_listed_nodes(
        [g for g in live.guests if is_live_rail_guest(g)],
        listed,
    )
    prev_guests = [
        g for g in (previous.guests if previous else []) if _is_pve_guest(g)
    ]

    by_id: dict[tuple[str, str, int], TopologyEntity] = {}
    by_name: dict[tuple[str, str, str], list[TopologyEntity]] = {}
    for g in prev_guests:
        by_id[_id_key(g)] = g
        if not _is_fallback_name(g):
            by_name.setdefault(_name_key(g), []).append(g)

    used_prev: set[str] = set()
    out: list[TopologyEntity] = []
    added = updated = unchanged = 0
    id_changes: list[tuple[str, str]] = []
    added_from: list[tuple[str, str]] = []

    for live_g in live_guests:
        prev = by_id.get(_id_key(live_g))
        if prev is None and not _is_fallback_name(live_g):
            candidates = by_name.get(_name_key(live_g), [])
            for cand in candidates:
                if cand.id not in used_prev:
                    prev = cand
                    break
        if prev is None:
            out.append(live_g)
            added += 1
            added_from.append((live_g.name, guest_source_label(live_g)))
            continue
        used_prev.add(prev.id)
        merged = _fill_ips_from_previous(live_g, prev)
        if prev.id != live_g.id:
            id_changes.append((prev.id, live_g.id))
            updated += 1
        elif _guest_fingerprint(merged) != _guest_fingerprint(prev):
            updated += 1
        else:
            unchanged += 1
        out.append(merged)

    removed_ids = tuple(
        g.id for g in prev_guests if g.id not in used_prev
    )
    hosts = list(live.hosts)
    if previous is not None and _manual_hosts_failed(live):
        hosts = list(previous.hosts)

    stats = ReconcileStats(
        added=added,
        updated=updated,
        removed=len(removed_ids),
        unchanged=unchanged,
        id_changes=tuple(id_changes),
        removed_ids=removed_ids,
        added_from=tuple(added_from),
    )
    snapshot = live.model_copy(
        update={
            "nodes": list(live.nodes),
            "guests": out,
            "hosts": hosts,
        }
    )
    return snapshot, stats
