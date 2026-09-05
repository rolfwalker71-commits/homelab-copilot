"""Hosts Online / Offline KPI — power state + optional Checks / Monitoring.

Counts LXC/QEMU guests and manual Linux hosts (same set as /mobile Hosts).
Unmonitored patcher targets are excluded from the split. Enabled health
checks that are ``down`` only flip a host when the check URL host matches
that entity's IP or hostname — never invent a host.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from app.core.models import EntityStatus, TopologyEntity, TopologySnapshot

logger = logging.getLogger(__name__)

BAGEL_C = 95.19
_ONLINE = {EntityStatus.RUNNING.value, EntityStatus.PAUSED.value, "online"}


def _as_status(value: Any) -> str:
    if value is None:
        return EntityStatus.UNKNOWN.value
    if isinstance(value, EntityStatus):
        return value.value
    raw = getattr(value, "value", value)
    return str(raw or EntityStatus.UNKNOWN.value).lower()


def _as_id(ent: Any) -> str:
    if isinstance(ent, dict):
        return str(ent.get("id") or "")
    return str(getattr(ent, "id", "") or "")


def _as_ips(ent: Any) -> list[str]:
    if isinstance(ent, dict):
        raw = ent.get("ip_addresses") or []
    else:
        raw = getattr(ent, "ip_addresses", None) or []
    return [str(ip).strip().lower() for ip in raw if ip]


def _as_hostname(ent: Any) -> str:
    if isinstance(ent, dict):
        hn = ent.get("hostname") or ""
    else:
        hn = getattr(ent, "hostname", None) or ""
    return str(hn).strip().lower().rstrip(".")


def is_power_online(status: Any) -> bool:
    """True for Proxmox/QEMU ``running`` / ``paused`` (same as /mobile isDown inverse)."""
    return _as_status(status) in _ONLINE


def snapshot_host_entities(snap: TopologySnapshot | None) -> list[Any]:
    """Guests + manual Linux hosts. Nodes and Docker are other KPIs."""
    if snap is None:
        return []
    return list(snap.guests or []) + list(snap.hosts or [])


def check_url_host(url: str) -> str:
    try:
        host = urlparse(str(url or "").strip()).hostname or ""
    except ValueError:
        return ""
    return host.strip().lower().rstrip(".")


def health_down_entity_ids(
    entities: list[Any],
    checks: list[dict[str, Any]] | None,
) -> set[str]:
    """Entity ids whose IP/hostname is the host of an enabled down check."""
    down_hosts: set[str] = set()
    for check in checks or []:
        if check.get("enabled") is False:
            continue
        if str(check.get("last_status") or "") != "down":
            continue
        host = check_url_host(str(check.get("url") or ""))
        if host:
            down_hosts.add(host)
    if not down_hosts:
        return set()
    ids: set[str] = set()
    for ent in entities:
        names = set(_as_ips(ent))
        hn = _as_hostname(ent)
        if hn:
            names.add(hn)
        if names & down_hosts:
            eid = _as_id(ent)
            if eid:
                ids.add(eid)
    return ids


def split_bagel_arcs(
    online: int,
    offline: int,
    *,
    circumference: float = BAGEL_C,
) -> dict[str, str]:
    """SVG dash arrays for a two-tone bagel. Lengths clamp to the ring."""
    on_n = max(0, int(online))
    off_n = max(0, int(offline))
    total = on_n + off_n
    if total <= 0 or circumference <= 0:
        empty = f"0.00 {circumference:.2f}"
        return {
            "online_dash": empty,
            "offline_dash": empty,
            "offline_offset": "0.00",
        }
    on_len = circumference * (on_n / total)
    off_len = circumference - on_len
    return {
        "online_dash": f"{on_len:.2f} {max(0.0, circumference - on_len):.2f}",
        "offline_dash": f"{off_len:.2f} {max(0.0, circumference - off_len):.2f}",
        "offline_offset": f"{-on_len:.2f}",
    }


def summarize_host_presence(
    entities: list[Any],
    *,
    unmonitored_ids: set[str] | None = None,
    health_down_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Online / offline split. Unmonitored ids are excluded, not guessed."""
    skip = {str(x) for x in (unmonitored_ids or set()) if x}
    down = {str(x) for x in (health_down_ids or set()) if x}
    online = 0
    offline = 0
    unmonitored = 0
    for ent in entities:
        eid = _as_id(ent)
        if eid and eid in skip:
            unmonitored += 1
            continue
        if (eid and eid in down) or not is_power_online(
            ent.get("status") if isinstance(ent, dict) else getattr(ent, "status", None)
        ):
            offline += 1
        else:
            online += 1
    arcs = split_bagel_arcs(online, offline)
    total = online + offline
    return {
        "online": online,
        "offline": offline,
        "unmonitored": unmonitored,
        "total": total,
        "center": f"{online} / {offline}",
        "warn": offline > 0,
        **arcs,
    }


async def host_presence_for_app(
    snap: TopologySnapshot | None,
    *,
    patcher_store: Any | None = None,
    health_store: Any | None = None,
) -> dict[str, Any]:
    """Dashboard helper: snapshot + optional patcher/health stores."""
    entities = snapshot_host_entities(snap)
    unmon: set[str] = set()
    checks: list[dict[str, Any]] = []
    if patcher_store is not None:
        try:
            unmon = set(await patcher_store.list_unmonitored_ids())
        except Exception:
            logger.exception("Hosts-KPI: Unmonitored-Liste nicht lesbar")
    if health_store is not None:
        try:
            checks = await health_store.list_checks(enabled_only=True)
        except Exception:
            logger.exception("Hosts-KPI: Health-Checks nicht lesbar")
    down_ids = health_down_entity_ids(entities, checks)
    return summarize_host_presence(
        entities,
        unmonitored_ids=unmon,
        health_down_ids=down_ids,
    )
