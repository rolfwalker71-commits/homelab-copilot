"""Zero-config auto-discovery: Proxmox REST API + Docker (socket / SSH).

Discovers nodes, LXC/QEMU guests, hostnames, IPs, and nested Docker containers.
Results are merged into a unified TopologySnapshot for the cache + dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import asyncssh
import httpx

from app.config import Settings
from app.core.locale import format_de, iso_utc, now_berlin
from app.core.models import EntityKind, EntityStatus, TopologyEntity, TopologySnapshot

logger = logging.getLogger(__name__)

_IP_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b"
)


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


class DiscoveryEngine:
    """Orchestrates Proxmox + Docker discovery into one topology snapshot."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def refresh(self) -> TopologySnapshot:
        now = now_berlin()
        errors: list[str] = []
        nodes: list[TopologyEntity] = []
        guests: list[TopologyEntity] = []
        containers: list[TopologyEntity] = []

        if self.settings.proxmox_configured:
            try:
                nodes, guests = await self._discover_proxmox()
            except Exception as exc:
                msg = f"Proxmox-Discovery fehlgeschlagen: {exc}"
                logger.exception(msg)
                errors.append(msg)
        else:
            errors.append(
                "Proxmox nicht konfiguriert — bitte PROXMOX_HOST und Token/Passwort setzen "
                "oder den Setup-Assistenten verwenden."
            )

        # Docker: local socket first, then SSH to discovered guest IPs
        try:
            local_ctrs = await self._discover_docker_local()
            containers.extend(local_ctrs)
        except Exception as exc:
            msg = f"Lokale Docker-Discovery fehlgeschlagen: {exc}"
            logger.warning(msg)
            errors.append(msg)

        ssh_targets = self._collect_ssh_targets(guests)
        if ssh_targets:
            remote = await self._discover_docker_ssh_many(ssh_targets)
            containers.extend(remote)

        return TopologySnapshot(
            refreshed_at=format_de(now),
            refreshed_at_iso=iso_utc(now),
            nodes=nodes,
            guests=guests,
            containers=containers,
            errors=errors,
            proxmox_configured=self.settings.proxmox_configured,
        )

    # ------------------------------------------------------------------
    # Proxmox
    # ------------------------------------------------------------------

    def _proxmox_headers(self) -> dict[str, str]:
        s = self.settings
        if s.proxmox_token_id and s.proxmox_token_secret:
            # token_id may be "user@realm!tokenname" or just "tokenname"
            token_id = s.proxmox_token_id
            if "!" not in token_id:
                token_id = f"{s.proxmox_user}!{token_id}"
            return {"Authorization": f"PVEAPIToken={token_id}={s.proxmox_token_secret}"}
        return {}

    async def _proxmox_ticket(self, client: httpx.AsyncClient) -> dict[str, str]:
        """Password auth fallback: obtain ticket + CSRF token."""
        s = self.settings
        resp = await client.post(
            f"{s.proxmox_base_url}/access/ticket",
            data={"username": s.proxmox_user, "password": s.proxmox_password},
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
        url = f"{self.settings.proxmox_base_url}{path}"
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json().get("data")

    async def _discover_proxmox(self) -> tuple[list[TopologyEntity], list[TopologyEntity]]:
        s = self.settings
        nodes: list[TopologyEntity] = []
        guests: list[TopologyEntity] = []
        stamp = format_de()
        stamp_iso = iso_utc()

        async with httpx.AsyncClient(
            verify=s.proxmox_verify_ssl,
            timeout=httpx.Timeout(20.0),
        ) as client:
            headers = self._proxmox_headers()
            if not headers and s.proxmox_password:
                headers = await self._proxmox_ticket(client)

            node_list = await self._proxmox_get(client, "/nodes", headers)
            for n in node_list or []:
                node_name = n.get("node", "")
                if s.proxmox_node and node_name != s.proxmox_node:
                    continue
                node_entity = TopologyEntity(
                    id=f"node:{node_name}",
                    kind=EntityKind.NODE,
                    name=node_name,
                    status=_status_from_str(n.get("status")),
                    node=node_name,
                    hostname=node_name,
                    meta={
                        "cpu": n.get("cpu"),
                        "maxcpu": n.get("maxcpu"),
                        "mem": n.get("mem"),
                        "maxmem": n.get("maxmem"),
                        "uptime": n.get("uptime"),
                    },
                    discovered_at=stamp,
                    discovered_at_iso=stamp_iso,
                )
                nodes.append(node_entity)

                # LXC
                try:
                    lxcs = await self._proxmox_get(
                        client, f"/nodes/{quote(node_name)}/lxc", headers
                    )
                    for ct in lxcs or []:
                        guests.append(
                            await self._enrich_guest(
                                client, headers, node_name, ct, EntityKind.LXC, stamp, stamp_iso
                            )
                        )
                except Exception as exc:
                    logger.warning("LXC list on %s failed: %s", node_name, exc)

                # QEMU (optional visibility; Docker typically runs in LXC)
                try:
                    vms = await self._proxmox_get(
                        client, f"/nodes/{quote(node_name)}/qemu", headers
                    )
                    for vm in vms or []:
                        guests.append(
                            await self._enrich_guest(
                                client, headers, node_name, vm, EntityKind.QEMU, stamp, stamp_iso
                            )
                        )
                except Exception as exc:
                    logger.warning("QEMU list on %s failed: %s", node_name, exc)

        return nodes, guests

    async def _enrich_guest(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        node: str,
        raw: dict[str, Any],
        kind: EntityKind,
        stamp: str,
        stamp_iso: str,
    ) -> TopologyEntity:
        vmid = int(raw.get("vmid", 0))
        name = raw.get("name") or f"{kind.value}-{vmid}"
        entity = TopologyEntity(
            id=f"{kind.value}:{node}:{vmid}",
            kind=kind,
            name=name,
            status=_status_from_str(raw.get("status")),
            node=node,
            vmid=vmid,
            hostname=name,
            parent_id=f"node:{node}",
            meta={"cpus": raw.get("cpus"), "maxmem": raw.get("maxmem"), "uptime": raw.get("uptime")},
            discovered_at=stamp,
            discovered_at_iso=stamp_iso,
        )

        if entity.status != EntityStatus.RUNNING:
            return entity

        # Pull agent / config network info for IPs
        kind_path = "lxc" if kind == EntityKind.LXC else "qemu"
        ips: list[str] = []
        try:
            if kind == EntityKind.LXC:
                cfg = await self._proxmox_get(
                    client, f"/nodes/{quote(node)}/lxc/{vmid}/config", headers
                )
                ips.extend(self._ips_from_lxc_config(cfg or {}))
            else:
                # QEMU guest agent interfaces (best-effort)
                ifaces = await self._proxmox_get(
                    client,
                    f"/nodes/{quote(node)}/qemu/{vmid}/agent/network-get-interfaces",
                    headers,
                )
                ips.extend(self._ips_from_qemu_agent(ifaces))
        except Exception as exc:
            logger.debug("IP enrichment for %s failed: %s", entity.id, exc)

        entity.ip_addresses = sorted(set(ips))
        return entity

    @staticmethod
    def _ips_from_lxc_config(cfg: dict[str, Any]) -> list[str]:
        ips: list[str] = []
        for key, val in cfg.items():
            if not str(key).startswith("net") or not isinstance(val, str):
                continue
            # e.g. name=eth0,bridge=vmbr0,ip=192.168.1.10/24,gw=...
            for part in val.split(","):
                part = part.strip()
                if part.startswith("ip=") and not part.startswith("ip=dhcp"):
                    addr = part[3:].split("/")[0]
                    if _IP_RE.fullmatch(addr) and not addr.startswith("127."):
                        ips.append(addr)
        return ips

    @staticmethod
    def _ips_from_qemu_agent(payload: Any) -> list[str]:
        ips: list[str] = []
        result = payload.get("result", payload) if isinstance(payload, dict) else payload
        if not isinstance(result, list):
            return ips
        for iface in result:
            for addr in iface.get("ip-addresses", []) or []:
                if addr.get("ip-address-type") != "ipv4":
                    continue
                ip = addr.get("ip-address", "")
                if _IP_RE.fullmatch(ip) and not ip.startswith("127."):
                    ips.append(ip)
        return ips

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
                    labels=dict(c.labels or {}),
                    meta={"short_id": c.short_id},
                    discovered_at=stamp,
                    discovered_at_iso=stamp_iso,
                )
            )
        return out

    # ------------------------------------------------------------------
    # Docker — remote via SSH (docker CLI on guest)
    # ------------------------------------------------------------------

    def _collect_ssh_targets(self, guests: list[TopologyEntity]) -> list[tuple[str, str]]:
        """Return (parent_id, ip) pairs for running guests with known IPs."""
        key = Path(self.settings.docker_ssh_key_path)
        if not key.is_file():
            logger.debug("SSH key not found at %s — skipping remote Docker scan", key)
            return []
        targets: list[tuple[str, str]] = []
        for g in guests:
            if g.status != EntityStatus.RUNNING or not g.ip_addresses:
                continue
            # Prefer first non-loopback IP
            targets.append((g.id, g.ip_addresses[0]))
        return targets

    async def _discover_docker_ssh_many(
        self, targets: list[tuple[str, str]]
    ) -> list[TopologyEntity]:
        sem = asyncio.Semaphore(6)

        async def one(parent_id: str, ip: str) -> list[TopologyEntity]:
            async with sem:
                try:
                    return await self._discover_docker_ssh(parent_id, ip)
                except Exception as exc:
                    logger.debug("SSH Docker scan %s (%s): %s", parent_id, ip, exc)
                    return []

        results = await asyncio.gather(*(one(pid, ip) for pid, ip in targets))
        merged: list[TopologyEntity] = []
        for batch in results:
            merged.extend(batch)
        return merged

    async def _discover_docker_ssh(self, parent_id: str, ip: str) -> list[TopologyEntity]:
        s = self.settings
        # Use `docker ps` JSON lines — works without Python docker SDK on guests.
        cmd = (
            "docker ps -a --format "
            "'{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}'"
        )
        async with asyncssh.connect(
            ip,
            port=s.docker_ssh_port,
            username=s.docker_ssh_user,
            client_keys=[s.docker_ssh_key_path],
            known_hosts=None,
            connect_timeout=s.docker_ssh_timeout,
        ) as conn:
            result = await conn.run(cmd, check=False)
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
            status = EntityStatus.RUNNING if status_raw.lower().startswith("up") else EntityStatus.STOPPED
            version = image.split(":")[-1] if ":" in image else None
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
                    meta={"via": "ssh", "raw_status": status_raw},
                    discovered_at=stamp,
                    discovered_at_iso=stamp_iso,
                )
            )
        return out
