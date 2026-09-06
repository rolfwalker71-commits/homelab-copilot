"""Zero-config auto-discovery: Proxmox REST API + Docker (socket / SSH).

Discovers nodes, LXC/QEMU guests, hostnames, IPs, and nested Docker containers.
Results are merged into a unified TopologySnapshot for the cache + dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import asyncssh
import httpx

from app.config import Settings
from app.core.locale import format_de, iso_utc, now_berlin
from app.core.models import EntityKind, EntityStatus, TopologyEntity, TopologySnapshot
from app.core.proxmox import (
    ProxmoxEndpoint,
    ProxmoxNodeUnboundError,
    format_proxmox_api_error,
    strip_unbound_metrics,
    unbound_message,
)
from app.core.pve_tag_colors import extract_color_map

logger = logging.getLogger(__name__)

_IP_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b"
)
_NON_STATIC_NET_IP = frozenset({"dhcp", "manual", "auto"})


def _usable_guest_ipv4(ip: str) -> bool:
    if not ip or not _IP_RE.fullmatch(ip):
        return False
    if ip.startswith("127.") or ip.startswith("169.254.") or ip == "0.0.0.0":
        return False
    return True


def prefer_guest_ipv4s(ips: list[str]) -> list[str]:
    """LAN ``192.168.`` first, then other global IPv4 (skip loopback / link-local)."""
    usable: list[str] = []
    seen: set[str] = set()
    for raw in ips:
        ip = str(raw or "").strip()
        if not ip or ip in seen or not _usable_guest_ipv4(ip):
            continue
        seen.add(ip)
        usable.append(ip)
    lan = [ip for ip in usable if ip.startswith("192.168.")]
    other = [ip for ip in usable if not ip.startswith("192.168.")]
    return lan + other


def _ipv4_tokens_from_value(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, (list, tuple)):
        out: list[str] = []
        for item in val:
            out.extend(_ipv4_tokens_from_value(item))
        return out
    text = str(val).strip()
    if not text:
        return []
    found: list[str] = []
    for token in re.split(r"[\s,;]+", text):
        addr = token.split("/")[0].strip()
        if _IP_RE.fullmatch(addr):
            found.append(addr)
    return found


def ips_from_lxc_config(cfg: dict[str, Any]) -> list[str]:
    """Static ``netN=...,ip=a.b.c.d/xx`` only — not ``dhcp`` / ``manual`` / ``auto``."""
    ips: list[str] = []
    for key, val in cfg.items():
        if not str(key).startswith("net") or not isinstance(val, str):
            continue
        ips.extend(_ips_from_ip_equals_parts(val))
    return prefer_guest_ipv4s(ips)


def ips_from_qemu_config(cfg: dict[str, Any]) -> list[str]:
    """Cloud-init ``ipconfigN=ip=a.b.c.d/xx`` (skip dhcp/manual)."""
    ips: list[str] = []
    for key, val in cfg.items():
        if not str(key).lower().startswith("ipconfig") or not isinstance(val, str):
            continue
        ips.extend(_ips_from_ip_equals_parts(val))
    return prefer_guest_ipv4s(ips)


def _ips_from_ip_equals_parts(val: str) -> list[str]:
    ips: list[str] = []
    for part in val.split(","):
        part = part.strip()
        if not part.lower().startswith("ip="):
            continue
        raw = part.split("=", 1)[1].strip()
        token = raw.split("/")[0].strip()
        if not token or token.lower() in _NON_STATIC_NET_IP:
            continue
        if _IP_RE.fullmatch(token):
            ips.append(token)
    return ips


def ips_from_iface_payload(payload: Any) -> list[str]:
    """PVE LXC ``/interfaces`` (inet / ip-addresses) or QEMU guest-agent ifaces."""
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("result", payload.get("data", payload))
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list):
        return []
    ips: list[str] = []
    for iface in rows:
        if not isinstance(iface, dict):
            continue
        ips.extend(_ipv4_tokens_from_value(iface.get("inet")))
        ips.extend(_ipv4_tokens_from_value(iface.get("ip")))
        for addr in iface.get("ip-addresses") or []:
            if not isinstance(addr, dict):
                ips.extend(_ipv4_tokens_from_value(addr))
                continue
            typ = str(addr.get("ip-address-type") or "").strip().lower()
            if typ and typ not in {"ipv4", "inet"}:
                continue
            ips.extend(_ipv4_tokens_from_value(addr.get("ip-address")))
    return prefer_guest_ipv4s(ips)


_RAIL_LIVE_STATUS = {
    EntityStatus.RUNNING,
    EntityStatus.STOPPED,
    EntityStatus.PAUSED,
}
_CONFIG_MISSING_HTTP = {404, 500, 595}


def _status_from_str(value: str | None) -> EntityStatus:
    if not value:
        return EntityStatus.UNKNOWN
    v = value.lower()
    if v in {"running", "online"}:
        return EntityStatus.RUNNING
    if v in {"stopped", "offline"}:
        return EntityStatus.STOPPED
    if v in {"paused", "suspended"}:
        return EntityStatus.PAUSED
    return EntityStatus.UNKNOWN


def _kind_value(kind: EntityKind | str | None) -> str:
    if isinstance(kind, EntityKind):
        return kind.value
    return str(kind or "").strip().lower()


def resource_guest_name(
    raw: dict[str, Any] | None,
    *,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Hostname from the resource row or config — never invent ``lxc-114``."""
    if isinstance(raw, dict):
        name = str(raw.get("name") or "").strip()
        if name:
            return name
    if isinstance(cfg, dict):
        name = str(cfg.get("hostname") or cfg.get("name") or "").strip()
        if name:
            return name
    return ""


def should_emit_rail_guest(
    *,
    kind: EntityKind | str,
    status: EntityStatus,
    template: bool = False,
    name: str | None = None,
    config_ok: bool = False,
    config_http: int | None = None,
) -> bool:
    """Hosts rail: live qemu/lxc only (not templates, unknown, or missing config)."""
    if _kind_value(kind) not in {"lxc", "qemu"}:
        return False
    if template:
        return False
    if config_http in _CONFIG_MISSING_HTTP:
        return False
    if status not in _RAIL_LIVE_STATUS:
        return False
    if not config_ok and not str(name or "").strip():
        return False
    return True


ManualHostsFn = Callable[[], Awaitable[list[dict[str, Any]]]]


class DiscoveryEngine:
    """Orchestrates Proxmox + Docker discovery into one topology snapshot."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._manual_hosts_fn: ManualHostsFn | None = None
        self._node_endpoints: dict[str, ProxmoxEndpoint] = {}
        self._unbound_via: dict[str, str] = {}
        self._discovery_done = False
        self._tag_style_cache: dict[str, tuple[float, dict[str, list[int]]]] = {}
        self._tag_style_ttl = 180.0

    def set_manual_hosts_provider(self, fn: ManualHostsFn | None) -> None:
        """Reuse the patcher host store (name, IP, SSH user/port)."""
        self._manual_hosts_fn = fn

    @staticmethod
    def _exc_text(exc: BaseException) -> str:
        """httpx timeouts often have empty str(); keep a useful message."""
        text = str(exc).strip()
        if text:
            return text
        cause = exc.__cause__ or exc.__context__
        if cause is not None:
            nested = str(cause).strip()
            if nested:
                return f"{type(exc).__name__}: {nested}"
        return type(exc).__name__

    @staticmethod
    def _is_docker_socket_permission_error(exc: BaseException) -> bool:
        """True when connecting to docker.sock failed with EACCES (possibly nested)."""
        cur: BaseException | None = exc
        seen: set[int] = set()
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            if isinstance(cur, PermissionError):
                return True
            if getattr(cur, "errno", None) == 13:
                return True
            text = str(cur).lower()
            if "permission denied" in text or "permissionerror(13" in text:
                return True
            nxt = cur.__cause__ or cur.__context__
            if isinstance(cur.args, tuple):
                for arg in cur.args:
                    if isinstance(arg, BaseException):
                        nxt = nxt or arg
                    elif isinstance(arg, tuple):
                        for inner in arg:
                            if isinstance(inner, BaseException):
                                nxt = nxt or inner
            cur = nxt if isinstance(nxt, BaseException) else None
        return False

    async def refresh(self) -> TopologySnapshot:
        now = now_berlin()
        errors: list[str] = []
        nodes: list[TopologyEntity] = []
        guests: list[TopologyEntity] = []
        hosts: list[TopologyEntity] = []
        containers: list[TopologyEntity] = []

        if self.settings.proxmox_configured:
            try:
                nodes, guests, px_errors = await self._discover_proxmox()
                errors.extend(px_errors)
            except Exception as exc:
                msg = f"Proxmox-Discovery fehlgeschlagen: {self._exc_text(exc)}"
                logger.exception(msg)
                errors.append(msg)
        else:
            errors.append(
                "Proxmox nicht konfiguriert — bitte im Setup speichern "
                "(Datenbank) oder optional PROXMOX_HOST und Token/Passwort setzen."
            )

        # Docker: local socket first (optional), then SSH to discovered guest IPs
        try:
            local_ctrs = await self._discover_docker_local()
            containers.extend(local_ctrs)
        except Exception as exc:
            if self._is_docker_socket_permission_error(exc):
                # Optional path — EACCES is noise when discovery is SSH-only.
                logger.info(
                    "Kein Zugriff auf docker.sock (%s) — lokal übersprungen "
                    "(DOCKER_USE_LOCAL_SOCKET=false oder User in Gruppe docker).",
                    self.settings.docker_socket,
                )
            else:
                msg = f"Lokale Docker-Discovery fehlgeschlagen: {self._exc_text(exc)}"
                logger.warning(msg)
                errors.append(msg)

        try:
            hosts = await self._load_manual_hosts()
        except Exception as exc:
            msg = f"Manuelle Hosts konnten nicht geladen werden: {self._exc_text(exc)}"
            logger.warning(msg)
            errors.append(msg)

        ssh_targets, ssh_skip = self._collect_ssh_targets(guests, hosts)
        if ssh_skip:
            errors.append(ssh_skip)
        if ssh_targets:
            remote, ssh_err = await self._discover_docker_ssh_many(ssh_targets)
            containers.extend(remote)
            if ssh_err:
                errors.append(ssh_err)

        return TopologySnapshot(
            refreshed_at=format_de(now),
            refreshed_at_iso=iso_utc(now),
            nodes=nodes,
            guests=guests,
            hosts=hosts,
            containers=containers,
            errors=errors,
            proxmox_configured=self.settings.proxmox_configured,
        )

    async def _load_manual_hosts(self) -> list[TopologyEntity]:
        if self._manual_hosts_fn is None:
            return []
        rows = await self._manual_hosts_fn()
        stamp = format_de()
        stamp_iso = iso_utc()
        out: list[TopologyEntity] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            hid = row.get("id")
            name = str(row.get("name") or "").strip()
            addr = str(row.get("host") or "").strip()
            if hid is None or not name or not addr:
                continue
            try:
                port = int(row.get("port") or self.settings.docker_ssh_port)
            except (TypeError, ValueError):
                port = self.settings.docker_ssh_port
            user = str(row.get("ssh_user") or "").strip()
            out.append(
                TopologyEntity(
                    id=f"manual:{int(hid)}",
                    kind=EntityKind.HOST,
                    name=name,
                    status=EntityStatus.RUNNING,
                    hostname=name,
                    ip_addresses=[addr],
                    meta={
                        "source": "manual",
                        "ssh_user": user,
                        "ssh_port": port,
                        "note": str(row.get("note") or ""),
                    },
                    discovered_at=stamp,
                    discovered_at_iso=stamp_iso,
                )
            )
        out.sort(key=lambda h: h.name.lower())
        return out

    # ------------------------------------------------------------------
    # Proxmox
    # ------------------------------------------------------------------

    @staticmethod
    def _client_base_url(client: httpx.AsyncClient, fallback: str = "") -> str:
        return str(getattr(client, "_pve_base_url", None) or fallback)

    def _attach_endpoint(self, client: httpx.AsyncClient, endpoint: ProxmoxEndpoint) -> None:
        setattr(client, "_pve_endpoint", endpoint)
        setattr(client, "_pve_base_url", endpoint.base_url)

    def _proxmox_headers(self, endpoint: ProxmoxEndpoint | None = None) -> dict[str, str]:
        ep = endpoint or (self.settings.proxmox_endpoints() or [None])[0]
        if ep is None:
            return {}
        return ep.auth_headers()

    async def _proxmox_ticket(
        self, client: httpx.AsyncClient, endpoint: ProxmoxEndpoint | None = None
    ) -> dict[str, str]:
        """Password auth fallback: obtain ticket + CSRF token."""
        ep = endpoint or getattr(client, "_pve_endpoint", None)
        if ep is None:
            s = self.settings
            ep = ProxmoxEndpoint(
                id="primary",
                host=s.proxmox_host,
                port=s.proxmox_port,
                user=s.proxmox_user,
                password=s.proxmox_password,
                verify_ssl=s.proxmox_verify_ssl,
            )
        resp = await client.post(
            f"{ep.base_url}/access/ticket",
            data={"username": ep.user, "password": ep.password},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return {
            "Cookie": f"PVEAuthCookie={data['ticket']}",
            "CSRFPreventionToken": data["CSRFPreventionToken"],
        }

    async def _proxmox_get(
        self, client: httpx.AsyncClient, path: str, headers: dict[str, str]
    ) -> Any:
        url = f"{self._client_base_url(client, self.settings.proxmox_base_url)}{path}"
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json().get("data")

    async def _proxmox_post(
        self,
        client: httpx.AsyncClient,
        path: str,
        headers: dict[str, str],
        data: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._client_base_url(client, self.settings.proxmox_base_url)}{path}"
        resp = await client.post(url, headers=headers, data=data or None)
        if resp.status_code == 403:
            raise PermissionError(
                "Keine Berechtigung für diese Aktion (HTTP 403). "
                "Dem API-Token fehlt vermutlich VM.PowerMgmt — "
                "Rolle PVEVMAdmin oder VM.PowerMgmt auf `/` (Propagate) zuweisen."
            )
        resp.raise_for_status()
        try:
            return resp.json().get("data")
        except Exception:
            return None

    async def _proxmox_delete(
        self,
        client: httpx.AsyncClient,
        path: str,
        headers: dict[str, str],
    ) -> Any:
        url = f"{self._client_base_url(client, self.settings.proxmox_base_url)}{path}"
        resp = await client.delete(url, headers=headers)
        if resp.status_code == 403:
            raise PermissionError(
                "Keine Berechtigung für Snapshots (HTTP 403). "
                "Dem API-Token fehlt VM.Snapshot — "
                "Rolle PVEVMAdmin oder VM.Snapshot auf `/` (Propagate) zuweisen."
            )
        resp.raise_for_status()
        try:
            return resp.json().get("data")
        except Exception:
            return None

    @staticmethod
    def _guest_id_parts(guest_id: str) -> tuple[str, str, int]:
        parts = guest_id.split(":")
        if len(parts) != 3 or parts[0] not in {"lxc", "qemu"}:
            raise ValueError(f"Ungültige Guest-ID: {guest_id}")
        try:
            vmid = int(parts[2])
        except ValueError as exc:
            raise ValueError(f"Ungültige Guest-ID: {guest_id}") from exc
        return parts[0], parts[1], vmid

    @staticmethod
    def _snapshot_acl_message(status_code: int) -> str:
        return (
            f"Keine Berechtigung für Snapshots (HTTP {status_code}). "
            "Dem API-Token fehlt VM.Snapshot — "
            "Rolle PVEVMAdmin oder VM.Snapshot auf `/` (Propagate) zuweisen."
        )

    async def _probe_token_acl(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        endpoint: ProxmoxEndpoint | None = None,
    ) -> list[str]:
        """Detect privilege-separated tokens without VM.Audit / Sys.Audit.

        Proxmox often returns HTTP 200 with an empty list for /lxc and /qemu when
        the token lacks ACL — that looks like "no guests" instead of 403.
        """
        hints: list[str] = []
        try:
            perms = await self._proxmox_get(client, "/access/permissions", headers)
        except Exception as exc:
            hints.append(
                f"Proxmox-Berechtigungen konnten nicht geprüft werden: {self._exc_text(exc)}"
            )
            return hints

        if not perms:
            hints.append(
                "API-Token hat keine effektiven Rechte (/access/permissions ist leer). "
                "Bei Privilege Separation erbt der Token NICHT die Rechte von root@pam. "
                "In der Proxmox-UI unter Datacenter → Permissions dem Token "
                f"{self._token_acl_subject(endpoint)} die Rolle PVEAuditor (oder VM.Audit + Sys.Audit) "
                "auf Pfad `/` mit Propagate zuweisen — danach Discovery erneut starten."
            )
            return hints

        flat = " ".join(
            " ".join(v) if isinstance(v, list) else str(v)
            for v in (perms.values() if isinstance(perms, dict) else [])
        )
        if "VM.Audit" not in flat and "Administrator" not in flat:
            hints.append(
                "API-Token fehlt VM.Audit — LXC/VM-Listen bleiben leer. "
                "Rolle PVEAuditor (oder VM.Audit) auf `/` für "
                f"{self._token_acl_subject(endpoint)} setzen."
            )
        if "Sys.Audit" not in flat and "Administrator" not in flat:
            hints.append(
                "API-Token fehlt Sys.Audit — Node-Details und Cluster-Status eingeschränkt. "
                "PVEAuditor auf `/` deckt Sys.Audit mit ab."
            )
        return hints

    def _token_acl_subject(self, endpoint: ProxmoxEndpoint | None = None) -> str:
        if endpoint is not None:
            return endpoint.token_acl_subject()
        s = self.settings
        tid = s.proxmox_token_id or "?"
        if "!" in tid:
            return tid
        return f"{s.proxmox_user}!{tid}"

    def remember_from_snapshot(self, snapshot: TopologySnapshot | None) -> None:
        """Hydrate node→API map from cached topology (before first refresh)."""
        if snapshot is None or self._discovery_done:
            return
        by_id = {ep.id: ep for ep in self.settings.proxmox_endpoints()}
        via_fallback = self._primary_via_label()
        for node in snapshot.nodes:
            name = (node.name or "").strip()
            if not name:
                continue
            meta = node.meta or {}
            if meta.get("api_unbound"):
                self._unbound_via[name] = str(meta.get("api_via") or via_fallback)
                continue
            ep_id = str(meta.get("pve_endpoint_id") or "")
            ep = by_id.get(ep_id)
            if ep is not None:
                self._node_endpoints[name] = ep

    def _primary_via_label(self) -> str:
        for name, ep in self._node_endpoints.items():
            if ep.id == "primary":
                return name
        return "dem verbundenen Host"

    def _require_endpoint_for_node(self, node: str) -> ProxmoxEndpoint:
        node = (node or "").strip()
        if node in self._unbound_via:
            raise ProxmoxNodeUnboundError(node, self._unbound_via[node])
        ep = self._node_endpoints.get(node)
        if ep is not None:
            return ep
        endpoints = self.settings.proxmox_endpoints()
        if not endpoints:
            raise RuntimeError("Proxmox ist nicht konfiguriert.")
        if not self._node_endpoints and len(endpoints) == 1:
            return endpoints[0]
        raise ProxmoxNodeUnboundError(node, self._primary_via_label())

    def _endpoint_for_guest_id(self, guest_id: str) -> ProxmoxEndpoint:
        _kind, node, _vmid = self._guest_id_parts(guest_id)
        return self._require_endpoint_for_node(node)

    async def _discover_proxmox(
        self,
    ) -> tuple[list[TopologyEntity], list[TopologyEntity], list[str]]:
        endpoints = self.settings.proxmox_endpoints()
        if not endpoints:
            return [], [], [
                "Keine Proxmox-Auth: im Setup Token/Passwort speichern "
                "oder optional PROXMOX_TOKEN_SECRET / PROXMOX_PASSWORD setzen."
            ]

        gathered = await asyncio.gather(
            *[self._discover_proxmox_endpoint(ep) for ep in endpoints],
            return_exceptions=True,
        )

        extra_owned: set[str] = set()
        parsed: list[tuple[ProxmoxEndpoint, list[TopologyEntity], list[TopologyEntity], list[str], set[str]]] = []
        errors: list[str] = []
        for ep, raw in zip(endpoints, gathered):
            if isinstance(raw, BaseException):
                msg = f"Proxmox {ep.host}: {format_proxmox_api_error(raw)}"
                logger.warning(msg)
                errors.append(msg)
                continue
            nodes_e, guests_e, errs_e, owned_e = raw
            parsed.append((ep, nodes_e, guests_e, errs_e, owned_e))
            if ep.id != "primary":
                extra_owned.update(owned_e)
            errors.extend(errs_e)

        nodes: list[TopologyEntity] = []
        guests: list[TopologyEntity] = []
        seen_node: set[str] = set()
        seen_guest: set[tuple[str, int]] = set()
        self._node_endpoints = {}
        self._unbound_via = {}

        for ep, nodes_e, guests_e, _errs, owned_e in parsed:
            for n in nodes_e:
                if ep.id == "primary" and n.name in extra_owned:
                    continue
                if n.name in seen_node:
                    continue
                seen_node.add(n.name)
                nodes.append(n)
                if n.name in owned_e:
                    self._node_endpoints[n.name] = ep
                elif (n.meta or {}).get("api_unbound"):
                    self._unbound_via[n.name] = str(
                        (n.meta or {}).get("api_via") or self._primary_via_label()
                    )
            for g in guests_e:
                if ep.id == "primary" and g.node and g.node in extra_owned:
                    continue
                key = (g.node or "", int(g.vmid or 0))
                if key in seen_guest:
                    continue
                seen_guest.add(key)
                guests.append(g)

        via = self._primary_via_label()
        owned = set(self._node_endpoints)
        listed = set(seen_node)
        guests = [
            g
            for g in guests
            if not (g.node or "").strip() or (g.node or "").strip() in listed
        ]
        for g in guests:
            name = (g.node or "").strip()
            if name and name not in owned:
                self._unbound_via[name] = str(
                    self._unbound_via.get(name) or via
                )

        self._discovery_done = True
        return nodes, guests, errors

    async def _probe_node_owned(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        node_name: str,
    ) -> tuple[bool, str | None]:
        """True when this API can serve ``GET /nodes/{node}/status``."""
        try:
            raw = await self._proxmox_get(
                client, f"/nodes/{quote(node_name)}/status", headers
            )
            return isinstance(raw, dict), None
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            detail = format_proxmox_api_error(exc)
            if code in {401, 403}:
                return True, detail
            return False, detail
        except Exception as exc:
            return False, format_proxmox_api_error(exc)

    async def _discover_proxmox_endpoint(
        self, ep: ProxmoxEndpoint
    ) -> tuple[list[TopologyEntity], list[TopologyEntity], list[str], set[str]]:
        nodes: list[TopologyEntity] = []
        guests: list[TopologyEntity] = []
        errors: list[str] = []
        owned_names: set[str] = set()
        stamp = format_de()
        stamp_iso = iso_utc()

        async with httpx.AsyncClient(
            verify=ep.verify_ssl,
            timeout=httpx.Timeout(20.0),
        ) as client:
            self._attach_endpoint(client, ep)
            headers = self._proxmox_headers(ep)
            if not headers and ep.password:
                headers = await self._proxmox_ticket(client, ep)
            if not headers:
                return nodes, guests, [
                    f"{ep.host}: Keine Proxmox-Auth — Token oder Passwort setzen."
                ], owned_names

            errors.extend(await self._probe_token_acl(client, headers, ep))

            node_list = await self._proxmox_get(client, "/nodes", headers)
            listed: list[tuple[str, dict[str, Any]]] = []
            for n in node_list or []:
                if not isinstance(n, dict):
                    continue
                node_name = str(n.get("node") or "").strip()
                if not node_name:
                    continue
                if ep.node_filter and node_name != ep.node_filter:
                    continue
                listed.append((node_name, n))

            if len(listed) == 1:
                owned_names.add(listed[0][0])
                probe_err: dict[str, str | None] = {}
            else:
                probe_err = {}
                for node_name, _raw in listed:
                    ok, detail = await self._probe_node_owned(client, headers, node_name)
                    if ok:
                        owned_names.add(node_name)
                    else:
                        probe_err[node_name] = detail

            via_label = next(iter(owned_names), "") or ep.host or "dem verbundenen Host"

            for node_name, raw in listed:
                owned = node_name in owned_names
                meta = self._node_resource_meta(raw)
                meta["pve_endpoint_id"] = ep.id if owned else ""
                meta["pve_endpoint"] = ep.host if owned else ""
                if not owned:
                    meta = strip_unbound_metrics(meta)
                    meta["api_unbound"] = True
                    meta["api_via"] = via_label
                    meta["api_error"] = unbound_message(via_label)
                    if probe_err.get(node_name):
                        meta["api_http"] = probe_err[node_name]
                    status = EntityStatus.UNKNOWN
                else:
                    status = _status_from_str(raw.get("status"))
                nodes.append(
                    TopologyEntity(
                        id=f"node:{node_name}",
                        kind=EntityKind.NODE,
                        name=node_name,
                        status=status,
                        node=node_name,
                        hostname=node_name,
                        meta=meta,
                        discovered_at=stamp,
                        discovered_at_iso=stamp_iso,
                    )
                )

            await self._enrich_node_ips(
                client, headers, [n for n in nodes if n.name in owned_names], endpoint=ep
            )

            seen_vmids: set[tuple[str, int]] = set()
            try:
                resources = await self._proxmox_get(
                    client, "/cluster/resources?type=vm", headers
                )
                for raw in resources or []:
                    if not isinstance(raw, dict):
                        continue
                    node_name = raw.get("node") or ""
                    if ep.node_filter and node_name != ep.node_filter:
                        continue
                    vmid = int(raw.get("vmid") or 0)
                    if not node_name or not vmid:
                        continue
                    rtype = (raw.get("type") or "").lower()
                    if rtype not in {"lxc", "qemu"}:
                        continue
                    kind = EntityKind.LXC if rtype == "lxc" else EntityKind.QEMU
                    if node_name in owned_names:
                        guest = await self._enrich_guest(
                            client, headers, node_name, raw, kind, stamp, stamp_iso
                        )
                    else:
                        guest = self._guest_entity_plain(
                            node_name, raw, kind, stamp, stamp_iso
                        )
                    if guest is None:
                        continue
                    seen_vmids.add((node_name, vmid))
                    guests.append(guest)
            except Exception as exc:
                msg = (
                    f"Cluster-Resources (VMs) auf {ep.host} fehlgeschlagen: "
                    f"{format_proxmox_api_error(exc)}"
                )
                logger.warning(msg)
                errors.append(msg)

            for node_name in sorted(owned_names):
                try:
                    lxcs = await self._proxmox_get(
                        client, f"/nodes/{quote(node_name)}/lxc", headers
                    )
                    for ct in lxcs or []:
                        if not isinstance(ct, dict):
                            continue
                        vmid = int(ct.get("vmid") or 0)
                        if not vmid or (node_name, vmid) in seen_vmids:
                            continue
                        guest = await self._enrich_guest(
                            client, headers, node_name, ct, EntityKind.LXC, stamp, stamp_iso
                        )
                        if guest is None:
                            continue
                        seen_vmids.add((node_name, vmid))
                        guests.append(guest)
                except Exception as exc:
                    msg = (
                        f"LXC-Liste auf Node {node_name} ({ep.host}) fehlgeschlagen: "
                        f"{format_proxmox_api_error(exc)}"
                    )
                    logger.warning(msg)
                    errors.append(msg)

                try:
                    vms = await self._proxmox_get(
                        client, f"/nodes/{quote(node_name)}/qemu", headers
                    )
                    for vm in vms or []:
                        if not isinstance(vm, dict):
                            continue
                        vmid = int(vm.get("vmid") or 0)
                        if not vmid or (node_name, vmid) in seen_vmids:
                            continue
                        guest = await self._enrich_guest(
                            client, headers, node_name, vm, EntityKind.QEMU, stamp, stamp_iso
                        )
                        if guest is None:
                            continue
                        seen_vmids.add((node_name, vmid))
                        guests.append(guest)
                except Exception as exc:
                    msg = (
                        f"QEMU-Liste auf Node {node_name} ({ep.host}) fehlgeschlagen: "
                        f"{format_proxmox_api_error(exc)}"
                    )
                    logger.warning(msg)
                    errors.append(msg)

            if (
                owned_names
                and not guests
                and not any("VM.Audit" in e or "keine effektiven Rechte" in e for e in errors)
            ):
                errors.append(
                    f"{ep.host}: Nodes gefunden, aber 0 Guests — vermutlich fehlende "
                    "Token-ACL (VM.Audit). Proxmox liefert dann HTTP 200 mit leerer Liste."
                )

        return nodes, guests, errors, owned_names

    def _guest_entity_plain(
        self,
        node: str,
        raw: dict[str, Any],
        kind: EntityKind,
        stamp: str,
        stamp_iso: str,
    ) -> TopologyEntity | None:
        """Guest row without config/agent (foreign node). No invented ``lxc-114``."""
        vmid = int(raw.get("vmid") or 0)
        name = resource_guest_name(raw)
        status = _status_from_str(raw.get("status") if isinstance(raw, dict) else None)
        template = bool(raw.get("template")) if isinstance(raw, dict) else False
        if not should_emit_rail_guest(
            kind=kind,
            status=status,
            template=template,
            name=name,
            config_ok=False,
        ):
            logger.info(
                "Hosts-Rail: %s %s/%s übersprungen (kein Config, Status=%s, Name=%r)",
                kind.value,
                node,
                vmid,
                status.value,
                name,
            )
            return None
        meta = self._guest_resource_meta(raw)
        meta["pve_source"] = node
        return TopologyEntity(
            id=f"{kind.value}:{node}:{vmid}",
            kind=kind,
            name=name,
            status=status,
            node=node,
            vmid=vmid,
            hostname=name,
            parent_id=f"node:{node}",
            meta=meta,
            discovered_at=stamp,
            discovered_at_iso=stamp_iso,
        )

    async def _enrich_node_ips(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        nodes: list[TopologyEntity],
        *,
        endpoint: ProxmoxEndpoint | None = None,
    ) -> None:
        """Attach SSH-reachable IPs to Proxmox nodes (cluster/status + API host)."""
        if not nodes:
            return
        s = self.settings
        by_name: dict[str, str] = {}

        try:
            status_rows = await self._proxmox_get(client, "/cluster/status", headers)
            for row in status_rows or []:
                if not isinstance(row, dict):
                    continue
                if (row.get("type") or "").lower() != "node":
                    continue
                name = (row.get("name") or row.get("node") or "").strip()
                ip = (row.get("ip") or "").strip()
                if name and ip:
                    by_name[name] = ip
        except Exception as exc:
            logger.debug("cluster/status for node IPs: %s", exc)

        mgmt = ((endpoint.host if endpoint else s.proxmox_host) or "").strip()
        node_filter = (endpoint.node_filter if endpoint else s.proxmox_node) or ""
        for node in nodes:
            if node.ip_addresses:
                continue
            if (node.meta or {}).get("api_unbound"):
                continue
            ip = by_name.get(node.name or "")
            if not ip and mgmt:
                if (
                    not node_filter
                    or node_filter == node.name
                    or len(nodes) == 1
                ):
                    ip = mgmt
            if ip:
                node.ip_addresses = [ip]
                node.meta = dict(node.meta or {})
                node.meta["ssh_ip"] = ip

    async def _fetch_guest_config(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        node: str,
        kind: EntityKind,
        vmid: int,
    ) -> tuple[dict[str, Any] | None, int | None]:
        """GET ``/nodes/{node}/{lxc|qemu}/{vmid}/config``. HTTP set on status errors."""
        path = f"/nodes/{quote(node)}/{kind.value}/{vmid}/config"
        try:
            cfg = await self._proxmox_get(client, path, headers)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            return None, int(code or 0) or None
        except Exception as exc:
            logger.debug("Config %s %s/%s: %s", kind.value, node, vmid, exc)
            return None, None
        if isinstance(cfg, dict):
            return cfg, 200
        return None, 200

    async def _enrich_guest(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        node: str,
        raw: dict[str, Any],
        kind: EntityKind,
        stamp: str,
        stamp_iso: str,
    ) -> TopologyEntity | None:
        vmid = int(raw.get("vmid", 0) or 0)
        status = _status_from_str(raw.get("status") if isinstance(raw, dict) else None)
        template = bool(raw.get("template")) if isinstance(raw, dict) else False
        cfg, config_http = await self._fetch_guest_config(
            client, headers, node, kind, vmid
        )
        config_ok = isinstance(cfg, dict) and config_http not in _CONFIG_MISSING_HTTP
        name = resource_guest_name(raw, cfg=cfg)
        if not should_emit_rail_guest(
            kind=kind,
            status=status,
            template=template,
            name=name,
            config_ok=config_ok,
            config_http=config_http,
        ):
            logger.info(
                "Hosts-Rail: %s %s/%s übersprungen (HTTP %s, Status=%s, Name=%r)",
                kind.value,
                node,
                vmid,
                config_http,
                status.value,
                name,
            )
            return None
        if not name:
            name = f"{kind.value}-{vmid}"
        meta = self._guest_resource_meta(raw)
        meta["pve_source"] = node
        entity = TopologyEntity(
            id=f"{kind.value}:{node}:{vmid}",
            kind=kind,
            name=name,
            status=status,
            node=node,
            vmid=vmid,
            hostname=name,
            parent_id=f"node:{node}",
            meta=meta,
            discovered_at=stamp,
            discovered_at_iso=stamp_iso,
        )

        if kind == EntityKind.LXC and isinstance(cfg, dict):
            self._apply_lxc_config_meta(entity.meta, cfg)
        elif kind == EntityKind.QEMU:
            if isinstance(cfg, dict) and not entity.meta.get("tags_list"):
                cfg_tags = cfg.get("tags")
                if cfg_tags:
                    tags_list = self._parse_proxmox_tags(cfg_tags)
                    entity.meta["tags"] = (
                        cfg_tags if isinstance(cfg_tags, str) else ";".join(tags_list)
                    )
                    entity.meta["tags_list"] = tags_list
        live_ips = await self._resolve_guest_ipv4s(
            client,
            headers,
            kind.value,
            node,
            vmid,
            cfg if isinstance(cfg, dict) else None,
            running=entity.status == EntityStatus.RUNNING,
        )
        if live_ips:
            entity.ip_addresses = live_ips

        return entity

    @staticmethod
    def _node_resource_meta(raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize Proxmox node list fields for the topology cache."""
        cpu = raw.get("cpu")
        mem = raw.get("mem")
        maxmem = raw.get("maxmem")
        maxcpu = raw.get("maxcpu")
        meta: dict[str, Any] = {
            "cpu": cpu,
            "maxcpu": maxcpu,
            "mem": mem,
            "maxmem": maxmem,
            "uptime": raw.get("uptime"),
            "disk": raw.get("disk"),
            "maxdisk": raw.get("maxdisk"),
        }
        try:
            if cpu is not None:
                meta["cpu_pct"] = round(max(0.0, float(cpu) * 100.0), 1)
        except (TypeError, ValueError):
            pass
        try:
            if mem is not None and maxmem and float(maxmem) > 0:
                meta["mem_pct"] = round(100.0 * float(mem) / float(maxmem), 1)
        except (TypeError, ValueError):
            pass
        try:
            disk = raw.get("disk")
            maxdisk = raw.get("maxdisk")
            if disk is not None and maxdisk and float(maxdisk) > 0:
                meta["disk_pct"] = round(100.0 * float(disk) / float(maxdisk), 1)
        except (TypeError, ValueError):
            pass
        return meta

    @staticmethod
    def _guest_resource_meta(raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize Proxmox guest resource fields for the detail header."""
        cpu = raw.get("cpu")
        mem = raw.get("mem")
        maxmem = raw.get("maxmem")
        disk = raw.get("disk")
        maxdisk = raw.get("maxdisk")
        tags_raw = raw.get("tags") or ""
        tags_list = DiscoveryEngine._parse_proxmox_tags(tags_raw)
        meta: dict[str, Any] = {
            "cpus": raw.get("cpus"),
            "cpu": cpu,
            "mem": mem,
            "maxmem": maxmem,
            "disk": disk,
            "maxdisk": maxdisk,
            "uptime": raw.get("uptime"),
            "netin": raw.get("netin"),
            "netout": raw.get("netout"),
            "template": bool(raw.get("template")),
            "tags": tags_raw if isinstance(tags_raw, str) else ";".join(tags_list),
            "tags_list": tags_list,
            "lock": raw.get("lock") or "",
        }
        try:
            if cpu is not None:
                meta["cpu_pct"] = round(max(0.0, float(cpu) * 100.0), 1)
        except (TypeError, ValueError):
            pass
        try:
            if mem is not None and maxmem and float(maxmem) > 0:
                meta["mem_pct"] = round(100.0 * float(mem) / float(maxmem), 1)
        except (TypeError, ValueError):
            pass
        try:
            if disk is not None and maxdisk and float(maxdisk) > 0:
                meta["disk_pct"] = round(100.0 * float(disk) / float(maxdisk), 1)
        except (TypeError, ValueError):
            pass
        return meta

    @staticmethod
    def _parse_proxmox_tags(raw: Any) -> list[str]:
        """Split Proxmox ``tags`` (semicolon-separated; occasionally comma)."""
        if raw is None:
            return []
        if isinstance(raw, (list, tuple)):
            parts = [str(x).strip() for x in raw]
        else:
            text = str(raw).strip()
            if not text:
                return []
            parts = re.split(r"[;,]", text)
        seen: set[str] = set()
        out: list[str] = []
        for p in parts:
            t = p.strip()
            if not t:
                continue
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
        return out

    @classmethod
    def _apply_lxc_config_meta(cls, meta: dict[str, Any], cfg: dict[str, Any]) -> None:
        unpriv = cfg.get("unprivileged")
        if unpriv is not None:
            meta["unprivileged"] = str(unpriv) in {"1", "true", "True"}
        onboot = cfg.get("onboot")
        if onboot is not None:
            meta["onboot"] = str(onboot) in {"1", "true", "True"}
        cfg_tags = cfg.get("tags")
        if cfg_tags:
            tags_list = cls._parse_proxmox_tags(cfg_tags)
            if tags_list:
                meta["tags"] = cfg_tags if isinstance(cfg_tags, str) else ";".join(tags_list)
                meta["tags_list"] = tags_list

    async def fetch_tag_color_map(
        self,
        *,
        node: str | None = None,
        guest_id: str | None = None,
        endpoint: ProxmoxEndpoint | None = None,
    ) -> dict[str, list[int]]:
        """Datacenter ``tag-style`` color-map for the guest's PVE API host.

        Cached a few minutes per endpoint so the dashboard does not hit
        ``GET /cluster/options`` on every guest click.
        """
        if endpoint is None:
            if guest_id:
                endpoint = self._endpoint_for_guest_id(guest_id)
            elif node:
                endpoint = self._require_endpoint_for_node(node)
            else:
                raise ValueError("guest_id oder node erforderlich")
        now = time.monotonic()
        cached = self._tag_style_cache.get(endpoint.id)
        if cached is not None and (now - cached[0]) < self._tag_style_ttl:
            return cached[1]
        mapping: dict[str, list[int]] = {}
        try:
            client, headers = await self._proxmox_authed_client(endpoint=endpoint)
            try:
                data = await self._proxmox_get(client, "/cluster/options", headers)
            finally:
                await client.aclose()
            mapping = extract_color_map(data)
        except Exception:
            logger.debug(
                "Proxmox tag-style (color-map) für %s nicht lesbar",
                endpoint.host,
                exc_info=True,
            )
        self._tag_style_cache[endpoint.id] = (now, mapping)
        return mapping

    async def fetch_all_tag_color_maps(self) -> dict[str, dict[str, list[int]]]:
        """Node name → color-map. One cached GET per distinct PVE host."""
        by_ep: dict[str, ProxmoxEndpoint] = {}
        nodes_for_ep: dict[str, list[str]] = {}
        for name, ep in self._node_endpoints.items():
            by_ep[ep.id] = ep
            nodes_for_ep.setdefault(ep.id, []).append(name)
        if not by_ep:
            endpoints = self.settings.proxmox_endpoints()
            if len(endpoints) == 1:
                by_ep[endpoints[0].id] = endpoints[0]
                nodes_for_ep[endpoints[0].id] = []
        maps_by_node: dict[str, dict[str, list[int]]] = {}
        for ep_id, ep in by_ep.items():
            cmap = await self.fetch_tag_color_map(endpoint=ep)
            names = nodes_for_ep.get(ep_id) or []
            if not names:
                maps_by_node["*"] = cmap
            for name in names:
                maps_by_node[name] = cmap
        return maps_by_node

    async def fetch_guest_rrd(
        self,
        guest_id: str,
        *,
        timeframe: str = "hour",
    ) -> dict[str, Any]:
        """Fetch Proxmox RRD samples for sparkline charts (selected guest only)."""
        if not self.settings.proxmox_configured:
            raise RuntimeError("Proxmox ist nicht konfiguriert.")
        parts = guest_id.split(":")
        if len(parts) != 3 or parts[0] not in {"lxc", "qemu"}:
            raise ValueError(f"Ungültige Guest-ID: {guest_id}")
        kind, node, vmid_s = parts
        try:
            vmid = int(vmid_s)
        except ValueError as exc:
            raise ValueError(f"Ungültige Guest-ID: {guest_id}") from exc
        if timeframe not in {"hour", "day", "week", "month", "year"}:
            timeframe = "hour"

        client, headers = await self._proxmox_authed_client(node=node)
        try:
            path = (
                f"/nodes/{quote(node)}/{kind}/{vmid}/rrddata"
                f"?timeframe={quote(timeframe)}&cf=AVERAGE"
            )
            rows = await self._proxmox_get(client, path, headers) or []
        finally:
            await client.aclose()

        cpu: list[float | None] = []
        net: list[float | None] = []
        times: list[int] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            t = row.get("time")
            if t is None:
                continue
            try:
                times.append(int(t))
            except (TypeError, ValueError):
                continue
            c = row.get("cpu")
            try:
                cpu.append(round(float(c) * 100.0, 2) if c is not None else None)
            except (TypeError, ValueError):
                cpu.append(None)
            ni, no = row.get("netin"), row.get("netout")
            try:
                n_val = 0.0
                if ni is not None:
                    n_val += float(ni)
                if no is not None:
                    n_val += float(no)
                net.append(n_val if (ni is not None or no is not None) else None)
            except (TypeError, ValueError):
                net.append(None)

        return {
            "guest_id": guest_id,
            "timeframe": timeframe,
            "time": times,
            "cpu": cpu,
            "net": net,
        }

    async def _proxmox_authed_client(
        self,
        *,
        node: str | None = None,
        endpoint: ProxmoxEndpoint | None = None,
    ) -> tuple[httpx.AsyncClient, dict[str, str]]:
        """Open an authenticated Proxmox HTTP client (caller must close)."""
        if not self.settings.proxmox_configured:
            raise RuntimeError("Proxmox ist nicht konfiguriert.")
        ep = endpoint
        if ep is None and node:
            ep = self._require_endpoint_for_node(node)
        if ep is None:
            endpoints = self.settings.proxmox_endpoints()
            if not endpoints:
                raise RuntimeError("Proxmox ist nicht konfiguriert.")
            ep = endpoints[0]
        client = httpx.AsyncClient(
            verify=ep.verify_ssl,
            timeout=httpx.Timeout(20.0, connect=8.0),
        )
        self._attach_endpoint(client, ep)
        try:
            headers = self._proxmox_headers(ep)
            if not headers and ep.password:
                headers = await self._proxmox_ticket(client, ep)
            if not headers:
                await client.aclose()
                raise RuntimeError("Keine Proxmox-Credentials verfügbar.")
            return client, headers
        except Exception:
            await client.aclose()
            raise

    async def fetch_node_status(self, node: str) -> dict[str, Any]:
        """Live ``GET /nodes/{node}/status`` normalized for the detail panel.

        Note: qemu/lxc use ``…/status/current``; nodes use ``…/status`` (no ``current``).
        """
        node = (node or "").strip()
        if not node or node.startswith("__"):
            raise ValueError(f"Ungültiger Node-Name: {node}")

        client, headers = await self._proxmox_authed_client(node=node)
        try:
            raw = await self._proxmox_get(
                client, f"/nodes/{quote(node)}/status", headers
            )
        finally:
            await client.aclose()

        if not isinstance(raw, dict):
            raise RuntimeError("Ungültige Antwort von Proxmox /nodes/.../status.")

        memory = raw.get("memory") if isinstance(raw.get("memory"), dict) else {}
        rootfs = raw.get("rootfs") if isinstance(raw.get("rootfs"), dict) else {}
        swap = raw.get("swap") if isinstance(raw.get("swap"), dict) else {}
        cpuinfo = raw.get("cpuinfo") if isinstance(raw.get("cpuinfo"), dict) else {}

        cpu = raw.get("cpu")
        mem_used = memory.get("used")
        mem_total = memory.get("total")
        root_used = rootfs.get("used")
        root_total = rootfs.get("total")
        root_avail = rootfs.get("avail") if rootfs.get("avail") is not None else rootfs.get("free")

        cpu_pct: float | None = None
        mem_pct: float | None = None
        rootfs_pct: float | None = None
        try:
            if cpu is not None:
                cpu_pct = round(max(0.0, float(cpu) * 100.0), 1)
        except (TypeError, ValueError):
            pass
        try:
            if mem_used is not None and mem_total and float(mem_total) > 0:
                mem_pct = round(100.0 * float(mem_used) / float(mem_total), 1)
        except (TypeError, ValueError):
            pass
        try:
            if root_used is not None and root_total and float(root_total) > 0:
                rootfs_pct = round(100.0 * float(root_used) / float(root_total), 1)
        except (TypeError, ValueError):
            pass

        loadavg = raw.get("loadavg")
        load_list: list[float] = []
        if isinstance(loadavg, (list, tuple)):
            for item in loadavg:
                try:
                    load_list.append(round(float(item), 2))
                except (TypeError, ValueError):
                    pass
        elif isinstance(loadavg, str):
            for part in loadavg.replace(",", " ").split():
                try:
                    load_list.append(round(float(part), 2))
                except (TypeError, ValueError):
                    pass

        wait_pct: float | None = None
        idle_pct: float | None = None
        try:
            if raw.get("wait") is not None:
                wait_pct = round(max(0.0, float(raw["wait"]) * 100.0), 1)
        except (TypeError, ValueError):
            pass
        try:
            if raw.get("idle") is not None:
                idle_pct = round(max(0.0, float(raw["idle"]) * 100.0), 1)
        except (TypeError, ValueError):
            pass

        cores = cpuinfo.get("cpus") or raw.get("maxcpu")
        try:
            cores = int(cores) if cores is not None else None
        except (TypeError, ValueError):
            cores = None

        return {
            "node": node,
            "uptime": raw.get("uptime"),
            "cpu": cpu,
            "cpu_pct": cpu_pct,
            "wait_pct": wait_pct,
            "idle_pct": idle_pct,
            "loadavg": load_list,
            "cores": cores,
            "cpu_model": cpuinfo.get("model") or "",
            "memory": {
                "used": mem_used,
                "total": mem_total,
                "free": memory.get("free"),
                "available": memory.get("available"),
                "pct": mem_pct,
            },
            "rootfs": {
                "used": root_used,
                "total": root_total,
                "avail": root_avail,
                "pct": rootfs_pct,
            },
            "swap": {
                "used": swap.get("used"),
                "total": swap.get("total"),
                "free": swap.get("free"),
            },
            "pveversion": raw.get("pveversion") or "",
            "kversion": raw.get("kversion") or "",
        }

    async def fetch_node_rrd(
        self,
        node: str,
        *,
        timeframe: str = "hour",
    ) -> dict[str, Any]:
        """Fetch Proxmox node RRD samples for CPU / memory / network charts."""
        node = (node or "").strip()
        if not node or node.startswith("__"):
            raise ValueError(f"Ungültiger Node-Name: {node}")
        if timeframe not in {"hour", "day", "week", "month", "year"}:
            timeframe = "hour"

        client, headers = await self._proxmox_authed_client(node=node)
        try:
            path = (
                f"/nodes/{quote(node)}/rrddata"
                f"?timeframe={quote(timeframe)}&cf=AVERAGE"
            )
            rows = await self._proxmox_get(client, path, headers) or []
        finally:
            await client.aclose()

        cpu: list[float | None] = []
        mem: list[float | None] = []
        net: list[float | None] = []
        times: list[int] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            t = row.get("time")
            if t is None:
                continue
            try:
                times.append(int(t))
            except (TypeError, ValueError):
                continue
            c = row.get("cpu")
            try:
                cpu.append(round(float(c) * 100.0, 2) if c is not None else None)
            except (TypeError, ValueError):
                cpu.append(None)
            # Node RRD exposes memused/memtotal (bytes), not a fraction field "mem".
            mem.append(self._node_rrd_mem_pct(row))
            ni, no = row.get("netin"), row.get("netout")
            try:
                n_val = 0.0
                if ni is not None:
                    n_val += float(ni)
                if no is not None:
                    n_val += float(no)
                net.append(n_val if (ni is not None or no is not None) else None)
            except (TypeError, ValueError):
                net.append(None)

        return {
            "node": node,
            "timeframe": timeframe,
            "time": times,
            "cpu": cpu,
            "mem": mem,
            "net": net,
        }

    @staticmethod
    def _node_rrd_mem_pct(row: dict[str, Any]) -> float | None:
        """Normalize node RRD memory to a 0–100 percentage."""
        try:
            used = row.get("memused")
            total = row.get("memtotal")
            if used is not None and total is not None and float(total) > 0:
                return round(100.0 * float(used) / float(total), 2)
            # Fallback for unusual/older shapes
            m = row.get("mem")
            if m is None:
                return None
            mv = float(m)
            return round(mv * 100.0 if mv <= 1.5 else mv, 2)
        except (TypeError, ValueError):
            return None

    async def fetch_node_storage(self, node: str) -> dict[str, Any]:
        """List storage pools on a node with used/total/avail."""
        node = (node or "").strip()
        if not node or node.startswith("__"):
            raise ValueError(f"Ungültiger Node-Name: {node}")

        client, headers = await self._proxmox_authed_client(node=node)
        try:
            rows = await self._proxmox_get(
                client, f"/nodes/{quote(node)}/storage", headers
            ) or []
        finally:
            await client.aclose()

        stores: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("storage") or row.get("name") or ""
            if not name:
                continue
            total = row.get("total")
            used = row.get("used")
            avail = row.get("avail")
            pct: float | None = None
            try:
                if used is not None and total and float(total) > 0:
                    pct = round(100.0 * float(used) / float(total), 1)
            except (TypeError, ValueError):
                pass
            active = row.get("active")
            enabled = row.get("enabled")
            stores.append(
                {
                    "storage": name,
                    "type": row.get("type") or "",
                    "content": row.get("content") or "",
                    "total": total,
                    "used": used,
                    "avail": avail,
                    "pct": pct,
                    "active": bool(active) if active is not None else True,
                    "enabled": bool(enabled) if enabled is not None else True,
                    "shared": bool(row.get("shared")) if row.get("shared") is not None else False,
                }
            )
        stores.sort(key=lambda s: str(s["storage"]).lower())
        return {"node": node, "storage": stores}

    def _guest_live_from_pve(
        self,
        guest_id: str,
        kind: str,
        node: str,
        vmid: int,
        status_raw: dict[str, Any],
        cfg: dict[str, Any] | None,
        *,
        ip_addresses: list[str] | None = None,
    ) -> dict[str, Any]:
        """Normalize ``status/current`` (+ optional config) for rail + card.

        Does not invent CPU/RAM/disk: percentages exist only when PVE sent
        the underlying fields. ``uptime`` comes from ``status/current``.
        """
        meta = self._guest_resource_meta(status_raw)
        if kind == "lxc" and isinstance(cfg, dict):
            self._apply_lxc_config_meta(meta, cfg)
        elif kind == "qemu" and isinstance(cfg, dict) and not meta.get("tags_list"):
            cfg_tags = cfg.get("tags")
            if cfg_tags:
                tags_list = self._parse_proxmox_tags(cfg_tags)
                meta["tags"] = (
                    cfg_tags if isinstance(cfg_tags, str) else ";".join(tags_list)
                )
                meta["tags_list"] = tags_list
        guest_status = str(status_raw.get("status") or "").strip().lower()
        ips = list(ip_addresses) if ip_addresses is not None else []
        if not ips and isinstance(cfg, dict):
            ips = self._static_guest_ipv4s(kind, cfg)
        return {
            "guest_id": guest_id,
            "kind": kind,
            "node": node,
            "vmid": vmid,
            "status": guest_status or "unknown",
            "uptime": meta.get("uptime"),
            "cpu": meta.get("cpu"),
            "cpu_pct": meta.get("cpu_pct"),
            "cpus": meta.get("cpus"),
            "mem": meta.get("mem"),
            "maxmem": meta.get("maxmem"),
            "mem_pct": meta.get("mem_pct"),
            "disk": meta.get("disk"),
            "maxdisk": meta.get("maxdisk"),
            "disk_pct": meta.get("disk_pct"),
            "lock": meta.get("lock") or "",
            "unprivileged": meta.get("unprivileged"),
            "name": resource_guest_name(status_raw, cfg=cfg) or "",
            "ip_addresses": ips,
        }

    async def _read_guest_live(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        guest_id: str,
        kind: str,
        node: str,
        vmid: int,
    ) -> dict[str, Any]:
        status_raw = await self._proxmox_get(
            client,
            f"/nodes/{quote(node)}/{kind}/{vmid}/status/current",
            headers,
        )
        if not isinstance(status_raw, dict):
            raise RuntimeError("Ungültige Antwort von Proxmox /status/current.")
        kind_enum = EntityKind.LXC if kind == "lxc" else EntityKind.QEMU
        cfg, _http = await self._fetch_guest_config(
            client, headers, node, kind_enum, vmid
        )
        cfg_dict = cfg if isinstance(cfg, dict) else None
        running = str(status_raw.get("status") or "").strip().lower() == "running"
        ips = await self._resolve_guest_ipv4s(
            client, headers, kind, node, vmid, cfg_dict, running=running
        )
        return self._guest_live_from_pve(
            guest_id,
            kind,
            node,
            vmid,
            status_raw,
            cfg_dict,
            ip_addresses=ips,
        )

    async def fetch_guest_status(self, guest_id: str) -> dict[str, Any]:
        """Live ``GET /nodes/{node}/{lxc|qemu}/{vmid}/status/current`` + config."""
        kind, node, vmid = self._guest_id_parts(guest_id)
        client, headers = await self._proxmox_authed_client(node=node)
        try:
            return await self._read_guest_live(
                client, headers, guest_id, kind, node, vmid
            )
        finally:
            await client.aclose()

    async def fetch_guests_status(self, guest_ids: list[str]) -> list[dict[str, Any]]:
        """Live power/metrics for several PVE guests (no full Discovery)."""

        async def one(gid: str) -> dict[str, Any]:
            try:
                live = await self.fetch_guest_status(gid)
                live["ok"] = True
                return live
            except Exception as exc:
                return {
                    "ok": False,
                    "guest_id": gid,
                    "error": str(exc) or type(exc).__name__,
                }

        ids = [str(g).strip() for g in guest_ids if str(g).strip()]
        if not ids:
            return []
        return list(await asyncio.gather(*[one(gid) for gid in ids]))

    async def fetch_guest_storage(self, guest_id: str) -> dict[str, Any]:
        """Disk usage + volume/mount assignment for an LXC or QEMU guest.

        Uses ``status/current`` for live disk gauges and ``config`` for rootfs/mp
        (LXC) or scsi/virtio/ide/sata disks (QEMU), including storage backend when
        readable from the volume spec.
        """
        parts = guest_id.split(":")
        if len(parts) != 3 or parts[0] not in {"lxc", "qemu"}:
            raise ValueError(f"Ungültige Guest-ID: {guest_id}")
        kind, node, vmid_s = parts
        try:
            vmid = int(vmid_s)
        except ValueError as exc:
            raise ValueError(f"Ungültige Guest-ID: {guest_id}") from exc

        client, headers = await self._proxmox_authed_client(node=node)
        try:
            status = await self._proxmox_get(
                client,
                f"/nodes/{quote(node)}/{kind}/{vmid}/status/current",
                headers,
            )
            cfg = await self._proxmox_get(
                client,
                f"/nodes/{quote(node)}/{kind}/{vmid}/config",
                headers,
            )
        finally:
            await client.aclose()

        if not isinstance(status, dict):
            status = {}
        if not isinstance(cfg, dict):
            cfg = {}

        disk_used = status.get("disk")
        disk_total = status.get("maxdisk")
        disk_pct: float | None = None
        try:
            if disk_used is not None and disk_total and float(disk_total) > 0:
                disk_pct = round(100.0 * float(disk_used) / float(disk_total), 1)
        except (TypeError, ValueError):
            pass

        volumes: list[dict[str, Any]] = []
        if kind == "lxc":
            volumes.extend(self._volumes_from_lxc_config(cfg))
        else:
            volumes.extend(self._volumes_from_qemu_config(cfg))

        return {
            "guest_id": guest_id,
            "kind": kind,
            "node": node,
            "vmid": vmid,
            "disk": {
                "used": disk_used,
                "total": disk_total,
                "pct": disk_pct,
            },
            "volumes": volumes,
        }

    async def fetch_guest_snapshots(self, guest_id: str) -> dict[str, Any]:
        """Read-only Proxmox snapshots for an LXC or QEMU guest.

        Missing ACL (403) is returned as a German hint, not a hard failure.
        """
        parts = guest_id.split(":")
        if len(parts) != 3 or parts[0] not in {"lxc", "qemu"}:
            raise ValueError(f"Ungültige Guest-ID: {guest_id}")
        kind, node, vmid_s = parts
        try:
            vmid = int(vmid_s)
        except ValueError as exc:
            raise ValueError(f"Ungültige Guest-ID: {guest_id}") from exc

        client, headers = await self._proxmox_authed_client(node=node)
        try:
            try:
                rows = await self._proxmox_get(
                    client,
                    f"/nodes/{quote(node)}/{kind}/{vmid}/snapshot",
                    headers,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    return {
                        "guest_id": guest_id,
                        "kind": kind,
                        "node": node,
                        "vmid": vmid,
                        "snapshots": [],
                        "acl_denied": True,
                        "message": (
                            "Keine Berechtigung für Snapshots (HTTP "
                            f"{exc.response.status_code}). Dem API-Token fehlt "
                            "vermutlich VM.Snapshot / VM.Audit."
                        ),
                    }
                raise
        finally:
            await client.aclose()

        snaps: list[dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            snaptime = row.get("snaptime")
            date_de = ""
            if snaptime is not None:
                try:
                    from datetime import datetime, timezone

                    dt = datetime.fromtimestamp(int(snaptime), tz=timezone.utc)
                    date_de = format_de(dt)
                except (TypeError, ValueError, OSError):
                    date_de = ""
            parent = str(row.get("parent") or "").strip()
            snaps.append(
                {
                    "name": name,
                    "description": str(row.get("description") or "").strip(),
                    "snaptime": snaptime,
                    "date": date_de,
                    "parent": parent or None,
                    "current": name == "current",
                }
            )
        from app.core.snapshots import build_snapshot_tree

        tree = build_snapshot_tree(snaps)
        return {
            "guest_id": guest_id,
            "kind": kind,
            "node": node,
            "vmid": vmid,
            "snapshots": tree,
            "acl_denied": False,
        }

    async def create_guest_snapshot(
        self,
        guest_id: str,
        *,
        name: str,
        description: str = "",
        prune_keep: int | None = None,
    ) -> dict[str, Any]:
        """Create a Proxmox snapshot. Never the node itself."""
        from app.core.snapshots import (
            SnapshotNameError,
            clamp_keep,
            guest_can_snapshot,
            snaps_to_delete,
            validate_snap_name,
        )

        if not guest_can_snapshot(guest_id):
            raise ValueError(
                "Snapshots nur für Proxmox-VM/LXC — nicht für den Node selbst."
            )
        try:
            snapname = validate_snap_name(name)
        except SnapshotNameError as exc:
            raise ValueError(exc.message) from exc
        kind, node, vmid = self._guest_id_parts(guest_id)
        client, headers = await self._proxmox_authed_client(node=node)
        try:
            try:
                upid = await self._proxmox_post(
                    client,
                    f"/nodes/{quote(node)}/{kind}/{vmid}/snapshot",
                    headers,
                    data={
                        "snapname": snapname,
                        "description": (description or "")[:200],
                    },
                )
            except PermissionError:
                raise PermissionError(self._snapshot_acl_message(403)) from None
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    raise PermissionError(
                        self._snapshot_acl_message(exc.response.status_code)
                    ) from exc
                detail = (exc.response.text or "")[:200]
                raise RuntimeError(
                    f"Snapshot anlegen fehlgeschlagen (HTTP {exc.response.status_code})"
                    + (f": {detail}" if detail else ".")
                ) from exc
            pruned: list[str] = []
            if prune_keep is not None:
                listed = await self.fetch_guest_snapshots(guest_id)
                if listed.get("acl_denied"):
                    logger.info("Retention übersprungen — keine Snapshot-Leserechte")
                else:
                    keep = clamp_keep(prune_keep)
                    for old in snaps_to_delete(listed.get("snapshots") or [], keep=keep):
                        try:
                            await self.delete_guest_snapshot(guest_id, old)
                            pruned.append(old)
                        except Exception:
                            logger.exception("Auto-Snapshot %s nicht gelöscht", old)
        finally:
            await client.aclose()
        kind_label = "LXC" if kind == "lxc" else "VM"
        return {
            "ok": True,
            "guest_id": guest_id,
            "name": snapname,
            "description": (description or "").strip(),
            "upid": upid,
            "pruned": pruned,
            "message": f"{kind_label} {vmid}: Snapshot „{snapname}“ wird angelegt…",
        }

    async def delete_guest_snapshot(self, guest_id: str, snapname: str) -> dict[str, Any]:
        from app.core.snapshots import SnapshotNameError, guest_can_snapshot, validate_snap_name

        if not guest_can_snapshot(guest_id):
            raise ValueError(
                "Snapshots nur für Proxmox-VM/LXC — nicht für den Node selbst."
            )
        try:
            name = validate_snap_name(snapname)
        except SnapshotNameError as exc:
            raise ValueError(exc.message) from exc
        if name == "current":
            raise ValueError("Der Marker „current“ kann nicht gelöscht werden.")
        kind, node, vmid = self._guest_id_parts(guest_id)
        client, headers = await self._proxmox_authed_client(node=node)
        try:
            try:
                upid = await self._proxmox_delete(
                    client,
                    f"/nodes/{quote(node)}/{kind}/{vmid}/snapshot/{quote(name)}",
                    headers,
                )
            except PermissionError:
                raise
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    raise PermissionError(
                        self._snapshot_acl_message(exc.response.status_code)
                    ) from exc
                detail = (exc.response.text or "")[:200]
                raise RuntimeError(
                    f"Snapshot löschen fehlgeschlagen (HTTP {exc.response.status_code})"
                    + (f": {detail}" if detail else ".")
                ) from exc
        finally:
            await client.aclose()
        return {
            "ok": True,
            "guest_id": guest_id,
            "name": name,
            "upid": upid,
            "message": f"Snapshot „{name}“ wird gelöscht…",
        }

    async def _wait_proxmox_task(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        node: str,
        upid: Any,
        *,
        timeout_s: float = 90.0,
    ) -> dict[str, Any] | None:
        """Poll a Proxmox UPID until stopped or timeout. None if there is no task id."""
        task_id = str(upid or "").strip()
        if not task_id or not task_id.startswith("UPID:"):
            return None
        deadline = asyncio.get_event_loop().time() + max(5.0, timeout_s)
        path = f"/nodes/{quote(node)}/tasks/{quote(task_id, safe='')}/status"
        last: dict[str, Any] | None = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await self._proxmox_get(client, path, headers)
            except Exception:
                await asyncio.sleep(1.2)
                continue
            if isinstance(raw, dict):
                last = raw
                if str(raw.get("status") or "").lower() == "stopped":
                    return last
            await asyncio.sleep(1.2)
        return last

    async def rollback_guest_snapshot(
        self, guest_id: str, snapname: str
    ) -> dict[str, Any]:
        """Rollback a guest to a named snapshot. Never automatic; never ``current``."""
        from app.core.snapshots import (
            SnapshotNameError,
            guest_can_snapshot,
            validate_snap_name,
        )

        if not guest_can_snapshot(guest_id):
            raise ValueError(
                "Rollback nur für Proxmox-VM/LXC — nicht für den Node selbst."
            )
        raw_name = (snapname or "").strip()
        if raw_name.lower() == "current":
            raise ValueError(
                "Der Marker „current“ (laufende Platte) kann nicht zurückgesetzt werden."
            )
        try:
            name = validate_snap_name(raw_name)
        except SnapshotNameError as exc:
            raise ValueError(exc.message) from exc

        kind, node, vmid = self._guest_id_parts(guest_id)
        kind_label = "LXC" if kind == "lxc" else "VM"
        client, headers = await self._proxmox_authed_client(node=node)
        task: dict[str, Any] | None = None
        upid: Any = None
        try:
            guest_status = ""
            try:
                st = await self._proxmox_get(
                    client,
                    f"/nodes/{quote(node)}/{kind}/{vmid}/status/current",
                    headers,
                )
                if isinstance(st, dict):
                    guest_status = str(st.get("status") or "").strip().lower()
            except Exception:
                guest_status = ""

            try:
                upid = await self._proxmox_post(
                    client,
                    f"/nodes/{quote(node)}/{kind}/{vmid}/snapshot/{quote(name)}/rollback",
                    headers,
                )
            except PermissionError:
                raise PermissionError(self._snapshot_acl_message(403)) from None
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    raise PermissionError(
                        self._snapshot_acl_message(exc.response.status_code)
                    ) from exc
                detail = ""
                try:
                    body = exc.response.json()
                    detail = str(
                        body.get("message")
                        or body.get("errors")
                        or body.get("data")
                        or body
                    )[:240]
                except Exception:
                    detail = (exc.response.text or "")[:240]
                low = detail.lower()
                if kind == "lxc" and (
                    "running" in low or "not stopped" in low or "isn't stopped" in low
                ):
                    raise RuntimeError(
                        f"LXC {vmid} läuft noch — Rollback braucht in der Regel "
                        "einen gestoppten Container. Bitte zuerst herunterfahren, "
                        "dann erneut zurücksetzen."
                    ) from exc
                raise RuntimeError(
                    f"Snapshot-Rollback fehlgeschlagen (HTTP {exc.response.status_code})"
                    + (f": {detail}" if detail else ".")
                ) from exc

            task = await self._wait_proxmox_task(client, headers, node, upid)
            if isinstance(task, dict):
                exitstatus = str(task.get("exitstatus") or "").strip()
                task_status = str(task.get("status") or "").strip().lower()
                if task_status == "stopped" and exitstatus and exitstatus.upper() != "OK":
                    low = exitstatus.lower()
                    if kind == "lxc" and (
                        "running" in low or "not stopped" in low
                    ):
                        raise RuntimeError(
                            f"LXC {vmid} läuft noch — Rollback braucht in der Regel "
                            "einen gestoppten Container. Bitte zuerst herunterfahren, "
                            "dann erneut zurücksetzen."
                        )
                    raise RuntimeError(
                        f"Snapshot-Rollback fehlgeschlagen: {exitstatus}"
                    )

            try:
                st = await self._proxmox_get(
                    client,
                    f"/nodes/{quote(node)}/{kind}/{vmid}/status/current",
                    headers,
                )
                if isinstance(st, dict):
                    guest_status = str(st.get("status") or "").strip().lower()
            except Exception:
                pass
        finally:
            await client.aclose()

        still_running = (
            isinstance(task, dict)
            and str(task.get("status") or "").strip().lower() != "stopped"
        )
        if still_running:
            message = (
                f"{kind_label} {vmid}: Rollback auf „{name}“ läuft noch "
                "(Proxmox-Task). Status gleich neu laden."
            )
        else:
            message = (
                f"{kind_label} {vmid}: auf Snapshot „{name}“ zurückgesetzt."
            )
        return {
            "ok": True,
            "guest_id": guest_id,
            "name": name,
            "kind": kind,
            "status": guest_status,
            "upid": upid,
            "message": message,
        }

    async def fetch_node_storage_health(self, node: str) -> dict[str, Any]:
        """ZFS / LVM-thin / SMART summary + existing storage plugins. Read-only."""
        from app.core.storage_health import (
            chip_level_from_pct,
            smart_chip,
            zfs_chip,
        )

        node = (node or "").strip()
        if not node or node.startswith("__"):
            raise ValueError(f"Ungültiger Node-Name: {node}")

        storage = await self.fetch_node_storage(node)
        stores = list(storage.get("storage") or [])
        zfs_pools: list[dict[str, Any]] = []
        thin_pools: list[dict[str, Any]] = []
        smart: list[dict[str, Any]] = []
        acl_notes: list[str] = []

        client, headers = await self._proxmox_authed_client(node=node)
        try:
            zfs_rows = await self._proxmox_get_optional(
                client, f"/nodes/{quote(node)}/disks/zfs", headers, acl_notes
            )
            for row in zfs_rows or []:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or "").strip()
                if not name:
                    continue
                size = row.get("size")
                alloc = row.get("alloc")
                pct = None
                try:
                    if alloc is not None and size and float(size) > 0:
                        pct = round(100.0 * float(alloc) / float(size), 1)
                except (TypeError, ValueError):
                    pass
                health = str(row.get("health") or "").strip()
                zfs_pools.append(
                    {
                        "name": name,
                        "health": health,
                        "size": size,
                        "alloc": alloc,
                        "free": row.get("free"),
                        "frag": row.get("frag"),
                        "pct": pct,
                        "chip": zfs_chip(health)
                        if health
                        else chip_level_from_pct(pct),
                    }
                )

            thin_rows = await self._proxmox_get_optional(
                client, f"/nodes/{quote(node)}/disks/lvmthin", headers, acl_notes
            )
            for row in thin_rows or []:
                if not isinstance(row, dict):
                    continue
                lv = str(row.get("lv") or row.get("name") or "").strip()
                if not lv:
                    continue
                used = row.get("used")
                total = row.get("total") or row.get("size")
                meta_used = row.get("metadata_used")
                meta_total = row.get("metadata_size") or row.get("metadata_total")
                pct = None
                meta_pct = None
                try:
                    if used is not None and total and float(total) > 0:
                        pct = round(100.0 * float(used) / float(total), 1)
                except (TypeError, ValueError):
                    pass
                try:
                    if meta_used is not None and meta_total and float(meta_total) > 0:
                        meta_pct = round(100.0 * float(meta_used) / float(meta_total), 1)
                except (TypeError, ValueError):
                    pass
                chip = chip_level_from_pct(
                    max(p for p in (pct, meta_pct) if p is not None)
                    if any(p is not None for p in (pct, meta_pct))
                    else None
                )
                thin_pools.append(
                    {
                        "name": lv,
                        "vg": row.get("vg") or "",
                        "used": used,
                        "total": total,
                        "pct": pct,
                        "metadata_used": meta_used,
                        "metadata_total": meta_total,
                        "metadata_pct": meta_pct,
                        "chip": chip,
                    }
                )

            disks = await self._proxmox_get_optional(
                client, f"/nodes/{quote(node)}/disks/list", headers, acl_notes
            )
            for row in disks or []:
                if not isinstance(row, dict):
                    continue
                dev = str(row.get("devpath") or row.get("osdid") or "").strip()
                if not dev:
                    continue
                health = str(row.get("health") or "").strip()
                wear = row.get("wearout")
                temp = row.get("temp") or row.get("temperature")
                failing = str(health).lower() in {"failed", "failing"}
                prefail = str(health).lower() in {"prefail", "pre-fail", "warning"}
                if not health and wear is None and temp is None:
                    continue
                smart.append(
                    {
                        "disk": dev.rsplit("/", 1)[-1],
                        "model": str(row.get("model") or "").strip(),
                        "health": health or "—",
                        "temp": temp,
                        "wearout": wear,
                        "chip": smart_chip(health, failing=failing, prefail=prefail),
                    }
                )
        finally:
            await client.aclose()

        return {
            "node": node,
            "storage": stores,
            "zfs": zfs_pools,
            "lvmthin": thin_pools,
            "smart": smart,
            "acl_notes": acl_notes,
        }

    async def _proxmox_get_optional(
        self,
        client: httpx.AsyncClient,
        path: str,
        headers: dict[str, str],
        acl_notes: list[str],
    ) -> list[Any]:
        try:
            data = await self._proxmox_get(client, path, headers)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                acl_notes.append(
                    f"{path}: keine Berechtigung (HTTP {exc.response.status_code})."
                )
                return []
            if exc.response.status_code in {404, 500, 501}:
                return []
            logger.debug("Proxmox GET %s: %s", path, exc)
            return []
        except Exception:
            logger.debug("Proxmox GET %s fehlgeschlagen", path, exc_info=True)
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return []

    async def guest_power(self, guest_id: str, action: str) -> dict[str, Any]:
        """Start / stop / shutdown / reboot an LXC or QEMU guest via Proxmox.

        Maps to ``POST /nodes/{node}/{lxc|qemu}/{vmid}/status/{action}``.
        Requires ``VM.PowerMgmt`` (403 → clear German ACL hint).
        """
        action = (action or "").strip().lower()
        allowed = {"start", "stop", "shutdown", "reboot"}
        if action not in allowed:
            raise ValueError(
                f"Ungültige Power-Aktion „{action}“. Erlaubt: {', '.join(sorted(allowed))}."
            )
        parts = guest_id.split(":")
        if len(parts) != 3 or parts[0] not in {"lxc", "qemu"}:
            raise ValueError(f"Ungültige Guest-ID: {guest_id}")
        kind, node, vmid_s = parts
        try:
            vmid = int(vmid_s)
        except ValueError as exc:
            raise ValueError(f"Ungültige Guest-ID: {guest_id}") from exc

        labels = {
            "start": "gestartet",
            "stop": "gestoppt",
            "shutdown": "heruntergefahren",
            "reboot": "neu gestartet",
        }
        kind_label = "LXC" if kind == "lxc" else "VM"
        already_msg = ""
        upid: Any = None
        live: dict[str, Any] | None = None
        client, headers = await self._proxmox_authed_client(node=node)
        try:
            try:
                upid = await self._proxmox_post(
                    client,
                    f"/nodes/{quote(node)}/{kind}/{vmid}/status/{action}",
                    headers,
                )
            except PermissionError:
                raise
            except httpx.HTTPStatusError as exc:
                detail = ""
                try:
                    body = exc.response.json()
                    detail = str(
                        body.get("message")
                        or body.get("errors")
                        or body.get("data")
                        or body
                    )[:240]
                except Exception:
                    detail = (exc.response.text or "")[:240]
                if exc.response.status_code == 403:
                    raise PermissionError(
                        "Keine Berechtigung für Power-Aktionen (HTTP 403). "
                        "Dem API-Token fehlt VM.PowerMgmt — "
                        "Rolle PVEVMAdmin oder VM.PowerMgmt auf `/` (Propagate) zuweisen."
                    ) from exc
                low = detail.lower()
                if "already running" in low:
                    already_msg = f"{kind_label} {vmid} läuft bereits."
                elif "not running" in low or "isn't running" in low:
                    already_msg = f"{kind_label} {vmid} ist nicht gestartet."
                else:
                    raise RuntimeError(
                        f"Proxmox Power-{action} fehlgeschlagen "
                        f"(HTTP {exc.response.status_code})"
                        + (f": {detail}" if detail else ".")
                    ) from exc
            try:
                live = await self._read_guest_live(
                    client, headers, guest_id, kind, node, vmid
                )
            except Exception:
                logger.debug(
                    "Live-Status nach Power %s für %s fehlgeschlagen",
                    action,
                    guest_id,
                    exc_info=True,
                )
        finally:
            await client.aclose()

        if already_msg:
            message = already_msg
        else:
            message = f"{kind_label} {vmid} wird {labels[action]}…"
        return {
            "ok": True,
            "already": bool(already_msg),
            "guest_id": guest_id,
            "action": action,
            "upid": upid,
            "message": message,
            "live": live,
        }

    @staticmethod
    def _parse_size_to_bytes(raw: str | None) -> int | None:
        """Parse Proxmox size strings like ``8G``, ``512M``, ``1024K``."""
        if not raw:
            return None
        s = str(raw).strip().upper().replace(" ", "")
        if not s:
            return None
        mult = 1
        if s[-1] in "KMGT":
            mult = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[s[-1]]
            s = s[:-1]
        try:
            return int(float(s) * mult)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _parse_volume_spec(cls, key: str, raw: str, *, kind: str) -> dict[str, Any]:
        """Normalize a Proxmox disk/rootfs/mp config value into a volume row."""
        parts = [p.strip() for p in str(raw).split(",") if p.strip()]
        head = parts[0] if parts else ""
        opts: dict[str, str] = {}
        for part in parts[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                opts[k.strip()] = v.strip()

        storage = ""
        volume = ""
        bind_path = ""
        if ":" in head and not head.startswith("/"):
            storage, volume = head.split(":", 1)
        elif head.startswith("/"):
            bind_path = head
            volume = head
        else:
            volume = head

        size_raw = opts.get("size") or ""
        size_bytes = cls._parse_size_to_bytes(size_raw)
        mp = opts.get("mp") or ""
        if key == "rootfs" and not mp:
            mp = "/"

        label = key
        if kind == "lxc":
            if key == "rootfs":
                label = "Rootfs"
            elif key.startswith("mp"):
                label = f"Mount {key}"
            else:
                label = key
        else:
            label = key

        return {
            "key": key,
            "label": label,
            "storage": storage,
            "volume": volume,
            "bind_path": bind_path,
            "mountpoint": mp,
            "size": size_raw,
            "size_bytes": size_bytes,
            "backup": opts.get("backup"),
            "raw": raw,
            "type": (
                "rootfs"
                if key == "rootfs"
                else "mp"
                if key.startswith("mp")
                else "disk"
            ),
        }

    @classmethod
    def _volumes_from_lxc_config(cls, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        vols: list[dict[str, Any]] = []
        rootfs = cfg.get("rootfs")
        if isinstance(rootfs, str) and rootfs.strip():
            vols.append(cls._parse_volume_spec("rootfs", rootfs, kind="lxc"))
        mp_keys = sorted(
            (k for k in cfg if re.fullmatch(r"mp\d+", str(k))),
            key=lambda k: int(str(k)[2:]) if str(k)[2:].isdigit() else 0,
        )
        for key in mp_keys:
            val = cfg.get(key)
            if isinstance(val, str) and val.strip():
                vols.append(cls._parse_volume_spec(str(key), val, kind="lxc"))
        return vols

    @classmethod
    def _volumes_from_qemu_config(cls, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """QEMU disk keys: scsi/virtio/ide/sata/efidisk/tpmstate/unused."""
        pattern = re.compile(
            r"^(scsi|virtio|ide|sata|efidisk|tpmstate|unused)\d+$",
            re.IGNORECASE,
        )
        keys = sorted(
            (k for k in cfg if pattern.match(str(k))),
            key=lambda k: str(k).lower(),
        )
        vols: list[dict[str, Any]] = []
        for key in keys:
            val = cfg.get(key)
            if isinstance(val, str) and val.strip():
                # Skip CD-ROM media entries without a real disk volume
                low = val.lower()
                if "media=cdrom" in low and ":" not in val.split(",")[0]:
                    continue
                vols.append(cls._parse_volume_spec(str(key), val, kind="qemu"))
        return vols

    @staticmethod
    def _static_guest_ipv4s(kind: str, cfg: dict[str, Any] | None) -> list[str]:
        if not isinstance(cfg, dict):
            return []
        if kind == "lxc":
            return ips_from_lxc_config(cfg)
        if kind == "qemu":
            return ips_from_qemu_config(cfg)
        return []

    async def _resolve_guest_ipv4s(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        kind: str,
        node: str,
        vmid: int,
        cfg: dict[str, Any] | None,
        *,
        running: bool,
    ) -> list[str]:
        """Static config IP, else live PVE interfaces / QEMU guest-agent."""
        static = self._static_guest_ipv4s(kind, cfg)
        if static:
            return static
        if not running:
            return []
        if kind == "lxc":
            path = f"/nodes/{quote(node)}/lxc/{vmid}/interfaces"
        elif kind == "qemu":
            path = f"/nodes/{quote(node)}/qemu/{vmid}/agent/network-get-interfaces"
        else:
            return []
        try:
            payload = await self._proxmox_get(client, path, headers)
        except Exception as exc:
            logger.debug("Guest-IPs %s %s/%s: %s", kind, node, vmid, exc)
            return []
        return ips_from_iface_payload(payload)

    @staticmethod
    def _ips_from_lxc_config(cfg: dict[str, Any]) -> list[str]:
        return ips_from_lxc_config(cfg)

    @staticmethod
    def _ips_from_qemu_agent(payload: Any) -> list[str]:
        return ips_from_iface_payload(payload)

    # ------------------------------------------------------------------
    # Docker — local socket
    # ------------------------------------------------------------------

    async def _discover_docker_local(self) -> list[TopologyEntity]:
        if not self.settings.docker_use_local_socket:
            return []
        sock = Path(self.settings.docker_socket)
        if not sock.exists():
            logger.debug("Docker socket not present at %s", sock)
            return []

        def _sync_list() -> list[TopologyEntity]:
            import docker  # lazy — optional when socket missing

            client = docker.DockerClient(base_url=f"unix://{sock}")
            try:
                return self._containers_from_docker_client(client, parent_id="local:docker")
            finally:
                client.close()

        return await asyncio.to_thread(_sync_list)

    def _containers_from_docker_client(
        self, client: Any, *, parent_id: str, host_hint: str | None = None
    ) -> list[TopologyEntity]:
        stamp = format_de()
        stamp_iso = iso_utc()
        out: list[TopologyEntity] = []
        for c in client.containers.list(all=True):
            attrs = c.attrs or {}
            state = (attrs.get("State") or {}).get("Status") or c.status
            image = ""
            if c.image and c.image.tags:
                image = c.image.tags[0]
            elif attrs.get("Config", {}).get("Image"):
                image = attrs["Config"]["Image"]
            version = image.split(":")[-1] if ":" in image else None
            networks = (attrs.get("NetworkSettings") or {}).get("Networks") or {}
            ips = [
                n.get("IPAddress")
                for n in networks.values()
                if n.get("IPAddress")
            ]
            name = c.name.lstrip("/") if c.name else c.short_id
            labels = dict(c.labels or {})
            published = self._published_ports_from_inspect(attrs)
            meta: dict[str, Any] = {"short_id": c.short_id}
            if published:
                meta["published_ports"] = published
            if labels.get("com.docker.compose.project"):
                meta["compose_project"] = labels["com.docker.compose.project"]
            if labels.get("com.docker.compose.service"):
                meta["compose_service"] = labels["com.docker.compose.service"]
            out.append(
                TopologyEntity(
                    id=f"docker:{parent_id}:{c.short_id}",
                    kind=EntityKind.DOCKER,
                    name=name,
                    status=_status_from_str(state),
                    hostname=host_hint or name,
                    ip_addresses=ips,
                    parent_id=parent_id,
                    image=image or None,
                    version=version,
                    labels=labels,
                    meta=meta,
                    discovered_at=stamp,
                    discovered_at_iso=stamp_iso,
                )
            )
        return out

    # ------------------------------------------------------------------
    # Docker — remote via SSH (docker CLI on guest)
    # ------------------------------------------------------------------

    def _ssh_key_path(self) -> Path:
        """Resolve SSH key: configured path, else ``{data_dir}/ssh/id_ed25519``."""
        configured = Path(self.settings.docker_ssh_key_path)
        if configured.is_file():
            return configured
        fallback = Path(self.settings.data_dir) / "ssh" / "id_ed25519"
        if fallback.is_file():
            return fallback
        return configured

    def _collect_ssh_targets(
        self,
        guests: list[TopologyEntity],
        hosts: list[TopologyEntity] | None = None,
    ) -> tuple[list[tuple[str, str, int, str]], str | None]:
        """Return ((parent_id, ip, port, user) tuples, optional skip reason)."""
        s = self.settings
        candidates: list[tuple[str, str, int, str]] = []
        for g in guests:
            if g.status != EntityStatus.RUNNING or not g.ip_addresses:
                continue
            candidates.append(
                (g.id, g.ip_addresses[0], s.docker_ssh_port, s.docker_ssh_user)
            )
        for h in hosts or []:
            if not h.ip_addresses:
                continue
            meta = h.meta or {}
            try:
                port = int(meta.get("ssh_port") or s.docker_ssh_port)
            except (TypeError, ValueError):
                port = s.docker_ssh_port
            user = str(meta.get("ssh_user") or "").strip() or s.docker_ssh_user
            candidates.append((h.id, h.ip_addresses[0], port, user))

        if not candidates:
            return [], None

        key = self._ssh_key_path()
        if key.is_file():
            return candidates, None

        configured = Path(self.settings.docker_ssh_key_path)
        fallback = Path(self.settings.data_dir) / "ssh" / "id_ed25519"
        paths = str(configured)
        if str(fallback) != str(configured):
            paths = f"{configured} (Fallback {fallback} ebenfalls fehlend)"
        msg = (
            f"SSH-Key nicht gefunden unter {paths} — "
            f"Docker-Scan per SSH für {len(candidates)} Host(s) übersprungen. "
            f"Key in den Container mounten "
            f"(z. B. ./ssh/id_ed25519:/data/ssh/id_ed25519:ro) "
            f"oder DOCKER_SSH_KEY_PATH setzen."
        )
        logger.warning(msg)
        return [], msg

    async def _discover_docker_ssh_many(
        self, targets: list[tuple[str, str, int, str]]
    ) -> tuple[list[TopologyEntity], str | None]:
        """Scan guests via SSH with concurrency + overall time budget.

        Returns (containers, optional_warning). Unreachable hosts fail fast;
        unfinished targets after the budget are skipped so the HTTP request
        does not hang the browser.
        """
        s = self.settings
        sem = asyncio.Semaphore(s.docker_ssh_concurrency)
        per_host = s.docker_ssh_timeout
        budget = s.docker_ssh_budget_seconds
        logger.info(
            "SSH Docker scan: %d target(s), concurrency=%d, per_host=%.1fs, budget=%.1fs",
            len(targets),
            s.docker_ssh_concurrency,
            per_host,
            budget,
        )

        async def one(
            parent_id: str, ip: str, port: int, user: str
        ) -> list[TopologyEntity]:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self._discover_docker_ssh(parent_id, ip, port=port, username=user),
                        timeout=per_host,
                    )
                except asyncio.TimeoutError:
                    logger.debug("SSH Docker scan %s (%s): timed out after %.1fs", parent_id, ip, per_host)
                    return []
                except Exception as exc:
                    logger.debug("SSH Docker scan %s (%s): %s", parent_id, ip, exc)
                    return []

        tasks = [
            asyncio.create_task(one(pid, ip, port, user))
            for pid, ip, port, user in targets
        ]
        done, pending = await asyncio.wait(tasks, timeout=budget)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        merged: list[TopologyEntity] = []
        for t in done:
            try:
                merged.extend(t.result())
            except Exception as exc:
                logger.debug("SSH Docker scan task error: %s", exc)

        warn: str | None = None
        if pending:
            warn = (
                f"SSH-Docker-Scan Zeitbudget ({budget:.0f}s) erreicht — "
                f"{len(pending)} von {len(targets)} Host(s) übersprungen."
            )
            logger.warning(warn)
        else:
            logger.info("SSH Docker scan finished: %d container(s)", len(merged))
        return merged, warn

    async def _discover_docker_ssh(
        self,
        parent_id: str,
        ip: str,
        *,
        port: int | None = None,
        username: str | None = None,
    ) -> list[TopologyEntity]:
        s = self.settings
        # Include Compose project/service labels for dashboard stack grouping.
        cmd = (
            "docker ps -a --format "
            "'{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|"
            "{{.Label \"com.docker.compose.project\"}}|"
            "{{.Label \"com.docker.compose.service\"}}|"
            "{{.Ports}}'"
        )
        # Keep connect short; command gets the remaining per-host budget via wait_for.
        connect_timeout = min(2.0, s.docker_ssh_timeout)
        async with asyncssh.connect(
            ip,
            port=int(port or s.docker_ssh_port),
            username=(username or s.docker_ssh_user),
            client_keys=[str(self._ssh_key_path())],
            known_hosts=None,
            connect_timeout=connect_timeout,
            login_timeout=connect_timeout,
        ) as conn:
            result = await conn.run(cmd, check=False, timeout=max(1.0, s.docker_ssh_timeout - 0.5))
        if result.exit_status != 0:
            # Docker not installed / permission denied — not an error for topology
            return []

        stamp = format_de()
        stamp_iso = iso_utc()
        out: list[TopologyEntity] = []
        for line in (result.stdout or "").splitlines():
            line = line.strip().strip("'")
            if not line or "|" not in line:
                continue
            parts = line.split("|")
            if len(parts) < 4:
                continue
            cid, name, image, status_raw = parts[0], parts[1], parts[2], parts[3]
            compose_project = (parts[4].strip() if len(parts) > 4 else "") or ""
            compose_service = (parts[5].strip() if len(parts) > 5 else "") or ""
            ports_raw = (parts[6].strip() if len(parts) > 6 else "") or ""
            # Names can be comma-separated aliases — prefer the first.
            name = name.split(",", 1)[0].strip()
            status = EntityStatus.RUNNING if status_raw.lower().startswith("up") else EntityStatus.STOPPED
            version = image.split(":")[-1] if ":" in image else None
            labels: dict[str, str] = {}
            if compose_project:
                labels["com.docker.compose.project"] = compose_project
            if compose_service:
                labels["com.docker.compose.service"] = compose_service
            published = self._parse_published_ports(ports_raw)
            meta: dict[str, Any] = {"via": "ssh", "raw_status": status_raw}
            if compose_project:
                meta["compose_project"] = compose_project
            if compose_service:
                meta["compose_service"] = compose_service
            if published:
                meta["published_ports"] = published
            if ports_raw:
                meta["ports_raw"] = ports_raw
            out.append(
                TopologyEntity(
                    id=f"docker:{parent_id}:{cid[:12]}",
                    kind=EntityKind.DOCKER,
                    name=name,
                    status=status,
                    hostname=ip,
                    ip_addresses=[ip],
                    parent_id=parent_id,
                    image=image,
                    version=version,
                    labels=labels,
                    meta=meta,
                    discovered_at=stamp,
                    discovered_at_iso=stamp_iso,
                )
            )
        return out

    @staticmethod
    def _parse_published_ports(raw: str) -> list[dict[str, Any]]:
        """Parse ``docker ps`` Ports column into host-published bindings."""
        if not raw or raw == "-":
            return []
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, int, str]] = set()
        for m in re.finditer(
            r"(?:(?P<hip>\d+\.\d+\.\d+\.\d+|\[[^\]]+\]|\*):)?(?P<hport>\d+)"
            r"->(?P<cport>\d+)/(?P<proto>\w+)",
            raw,
        ):
            try:
                hport = int(m.group("hport"))
            except (TypeError, ValueError):
                continue
            hip = (m.group("hip") or "0.0.0.0").strip()
            proto = (m.group("proto") or "tcp").lower()
            key = (hip, hport, proto)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "host_ip": hip,
                    "host_port": hport,
                    "container_port": int(m.group("cport") or 0),
                    "proto": proto,
                }
            )
        return out

    @classmethod
    def _published_ports_from_inspect(cls, attrs: dict[str, Any]) -> list[dict[str, Any]]:
        ports = ((attrs.get("NetworkSettings") or {}).get("Ports") or {})
        if not isinstance(ports, dict):
            return []
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, int, str]] = set()
        for key, bindings in ports.items():
            if not bindings:
                continue
            proto = "tcp"
            cport = 0
            if isinstance(key, str) and "/" in key:
                cport_s, proto = key.split("/", 1)
                try:
                    cport = int(cport_s)
                except ValueError:
                    cport = 0
            for b in bindings if isinstance(bindings, list) else []:
                if not isinstance(b, dict):
                    continue
                try:
                    hport = int(b.get("HostPort") or 0)
                except (TypeError, ValueError):
                    continue
                if not hport:
                    continue
                hip = str(b.get("HostIp") or "0.0.0.0")
                key_t = (hip, hport, proto)
                if key_t in seen:
                    continue
                seen.add(key_t)
                out.append(
                    {
                        "host_ip": hip,
                        "host_port": hport,
                        "container_port": cport,
                        "proto": proto,
                    }
                )
        return out

    async def fetch_host_facts(
        self,
        ip: str,
        *,
        port: int | None = None,
        username: str | None = None,
    ) -> dict[str, Any]:
        """OS / uptime / disk via a short SSH probe (manual Linux hosts)."""
        s = self.settings
        cmd = (
            "set +e; "
            "os=$(. /etc/os-release 2>/dev/null; echo \"${PRETTY_NAME:-}\"); "
            "kern=$(uname -sr 2>/dev/null); "
            "up=$(awk '{print int($1)}' /proc/uptime 2>/dev/null); "
            "disk=$(df -P -B1 / 2>/dev/null | awk 'NR==2{print $3\" \"$2\" \"$5}'); "
            "printf 'os=%s\\nkernel=%s\\nuptime=%s\\ndisk=%s\\n' "
            "\"$os\" \"$kern\" \"$up\" \"$disk\""
        )
        connect_timeout = min(4.0, max(2.0, s.docker_ssh_timeout))
        async with asyncssh.connect(
            ip,
            port=int(port or s.docker_ssh_port),
            username=(username or s.docker_ssh_user),
            client_keys=[str(self._ssh_key_path())],
            known_hosts=None,
            connect_timeout=connect_timeout,
            login_timeout=connect_timeout,
        ) as conn:
            result = await conn.run(cmd, check=False, timeout=8.0)
        text = (result.stdout or "") if isinstance(result.stdout, str) else (
            (result.stdout or b"").decode("utf-8", errors="replace")
        )
        facts: dict[str, Any] = {"os": "", "kernel": "", "uptime": None, "disk": None}
        for line in text.splitlines():
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            if key == "os":
                facts["os"] = val
            elif key == "kernel":
                facts["kernel"] = val
            elif key == "uptime":
                try:
                    facts["uptime"] = int(float(val))
                except (TypeError, ValueError):
                    facts["uptime"] = None
            elif key == "disk":
                parts = val.split()
                if len(parts) >= 2:
                    try:
                        used = int(parts[0])
                        total = int(parts[1])
                        pct = None
                        if len(parts) >= 3:
                            pct = float(parts[2].rstrip("%"))
                        elif total > 0:
                            pct = round(100.0 * used / total, 1)
                        facts["disk"] = {"used": used, "total": total, "pct": pct}
                    except (TypeError, ValueError):
                        facts["disk"] = None
        return facts
