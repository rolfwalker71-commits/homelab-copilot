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
                "Proxmox nicht konfiguriert — bitte PROXMOX_HOST und Token/Passwort setzen "
                "oder den Setup-Assistenten verwenden."
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

        ssh_targets, ssh_skip = self._collect_ssh_targets(guests)
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

    async def _proxmox_post(
        self,
        client: httpx.AsyncClient,
        path: str,
        headers: dict[str, str],
        data: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.settings.proxmox_base_url}{path}"
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

    async def _probe_token_acl(
        self, client: httpx.AsyncClient, headers: dict[str, str]
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
                f"{self._token_acl_subject()} die Rolle PVEAuditor (oder VM.Audit + Sys.Audit) "
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
                f"{self._token_acl_subject()} setzen."
            )
        if "Sys.Audit" not in flat and "Administrator" not in flat:
            hints.append(
                "API-Token fehlt Sys.Audit — Node-Details und Cluster-Status eingeschränkt. "
                "PVEAuditor auf `/` deckt Sys.Audit mit ab."
            )
        return hints

    def _token_acl_subject(self) -> str:
        s = self.settings
        tid = s.proxmox_token_id or "?"
        if "!" in tid:
            return tid
        return f"{s.proxmox_user}!{tid}"

    async def _discover_proxmox(
        self,
    ) -> tuple[list[TopologyEntity], list[TopologyEntity], list[str]]:
        s = self.settings
        nodes: list[TopologyEntity] = []
        guests: list[TopologyEntity] = []
        errors: list[str] = []
        stamp = format_de()
        stamp_iso = iso_utc()

        async with httpx.AsyncClient(
            verify=s.proxmox_verify_ssl,
            timeout=httpx.Timeout(20.0),
        ) as client:
            headers = self._proxmox_headers()
            if not headers and s.proxmox_password:
                headers = await self._proxmox_ticket(client)
            if not headers:
                return nodes, guests, [
                    "Keine Proxmox-Auth: PROXMOX_TOKEN_SECRET oder PROXMOX_PASSWORD setzen."
                ]

            # Surface ACL issues early (empty guest lists are usually permissions, not parsing)
            errors.extend(await self._probe_token_acl(client, headers))

            node_list = await self._proxmox_get(client, "/nodes", headers)
            node_names: list[str] = []
            for n in node_list or []:
                node_name = n.get("node", "")
                if not node_name:
                    continue
                if s.proxmox_node and node_name != s.proxmox_node:
                    continue
                node_names.append(node_name)
                node_entity = TopologyEntity(
                    id=f"node:{node_name}",
                    kind=EntityKind.NODE,
                    name=node_name,
                    status=_status_from_str(n.get("status")),
                    node=node_name,
                    hostname=node_name,
                    meta=self._node_resource_meta(n if isinstance(n, dict) else {}),
                    discovered_at=stamp,
                    discovered_at_iso=stamp_iso,
                )
                nodes.append(node_entity)

            await self._enrich_node_ips(client, headers, nodes)

            # Prefer cluster/resources — covers all nodes in one call when ACL allows
            seen_vmids: set[tuple[str, int]] = set()
            try:
                resources = await self._proxmox_get(
                    client, "/cluster/resources?type=vm", headers
                )
                for raw in resources or []:
                    node_name = raw.get("node") or ""
                    if s.proxmox_node and node_name != s.proxmox_node:
                        continue
                    vmid = int(raw.get("vmid") or 0)
                    if not node_name or not vmid:
                        continue
                    rtype = (raw.get("type") or "").lower()
                    kind = EntityKind.LXC if rtype == "lxc" else EntityKind.QEMU
                    seen_vmids.add((node_name, vmid))
                    guests.append(
                        await self._enrich_guest(
                            client, headers, node_name, raw, kind, stamp, stamp_iso
                        )
                    )
            except Exception as exc:
                msg = f"Cluster-Resources (VMs) fehlgeschlagen: {self._exc_text(exc)}"
                logger.warning(msg)
                errors.append(msg)

            for node_name in node_names:
                # LXC (per-node; fills gaps if cluster/resources was empty/denied)
                try:
                    lxcs = await self._proxmox_get(
                        client, f"/nodes/{quote(node_name)}/lxc", headers
                    )
                    for ct in lxcs or []:
                        vmid = int(ct.get("vmid") or 0)
                        if (node_name, vmid) in seen_vmids:
                            continue
                        seen_vmids.add((node_name, vmid))
                        guests.append(
                            await self._enrich_guest(
                                client, headers, node_name, ct, EntityKind.LXC, stamp, stamp_iso
                            )
                        )
                except Exception as exc:
                    msg = (
                        f"LXC-Liste auf Node {node_name} fehlgeschlagen: "
                        f"{self._exc_text(exc)}"
                    )
                    logger.warning(msg)
                    errors.append(msg)

                # QEMU
                try:
                    vms = await self._proxmox_get(
                        client, f"/nodes/{quote(node_name)}/qemu", headers
                    )
                    for vm in vms or []:
                        vmid = int(vm.get("vmid") or 0)
                        if (node_name, vmid) in seen_vmids:
                            continue
                        seen_vmids.add((node_name, vmid))
                        guests.append(
                            await self._enrich_guest(
                                client, headers, node_name, vm, EntityKind.QEMU, stamp, stamp_iso
                            )
                        )
                except Exception as exc:
                    msg = (
                        f"QEMU-Liste auf Node {node_name} fehlgeschlagen: "
                        f"{self._exc_text(exc)}"
                    )
                    logger.warning(msg)
                    errors.append(msg)

            if nodes and not guests and not any("VM.Audit" in e or "keine effektiven Rechte" in e for e in errors):
                errors.append(
                    "Nodes gefunden, aber 0 Guests — vermutlich fehlende Token-ACL "
                    "(VM.Audit). Proxmox liefert dann HTTP 200 mit leerer Liste."
                )

        return nodes, guests, errors

    async def _enrich_node_ips(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        nodes: list[TopologyEntity],
    ) -> None:
        """Attach SSH-reachable IPs to Proxmox nodes (cluster/status + PROXMOX_HOST)."""
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

        mgmt = (s.proxmox_host or "").strip()
        for node in nodes:
            if node.ip_addresses:
                continue
            ip = by_name.get(node.name or "")
            if not ip and mgmt:
                if (
                    not s.proxmox_node
                    or s.proxmox_node == node.name
                    or len(nodes) == 1
                ):
                    ip = mgmt
            if ip:
                node.ip_addresses = [ip]
                node.meta = dict(node.meta or {})
                node.meta["ssh_ip"] = ip

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
            meta=self._guest_resource_meta(raw),
            discovered_at=stamp,
            discovered_at_iso=stamp_iso,
        )

        # Config for tags / LXC nets (also when stopped — tags still useful in UI)
        try:
            if kind == EntityKind.LXC:
                cfg = await self._proxmox_get(
                    client, f"/nodes/{quote(node)}/lxc/{vmid}/config", headers
                )
                self._apply_lxc_config_meta(entity.meta, cfg or {})
                if entity.status == EntityStatus.RUNNING:
                    entity.ip_addresses = sorted(
                        set(self._ips_from_lxc_config(cfg or {}))
                    )
            elif kind == EntityKind.QEMU:
                if not entity.meta.get("tags_list"):
                    cfg = await self._proxmox_get(
                        client, f"/nodes/{quote(node)}/qemu/{vmid}/config", headers
                    )
                    cfg_tags = (cfg or {}).get("tags")
                    if cfg_tags:
                        tags_list = self._parse_proxmox_tags(cfg_tags)
                        entity.meta["tags"] = (
                            cfg_tags if isinstance(cfg_tags, str) else ";".join(tags_list)
                        )
                        entity.meta["tags_list"] = tags_list
                if entity.status == EntityStatus.RUNNING:
                    try:
                        ifaces = await self._proxmox_get(
                            client,
                            f"/nodes/{quote(node)}/qemu/{vmid}/agent/network-get-interfaces",
                            headers,
                        )
                        entity.ip_addresses = sorted(
                            set(self._ips_from_qemu_agent(ifaces))
                        )
                    except Exception as exc:
                        logger.debug("QEMU agent IPs for %s failed: %s", entity.id, exc)
        except Exception as exc:
            logger.debug("Config enrichment for %s failed: %s", entity.id, exc)

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

        async with httpx.AsyncClient(
            verify=self.settings.proxmox_verify_ssl,
            timeout=httpx.Timeout(20.0, connect=8.0),
        ) as client:
            headers = self._proxmox_headers()
            if not headers and self.settings.proxmox_password:
                headers = await self._proxmox_ticket(client)
            if not headers:
                raise RuntimeError("Keine Proxmox-Credentials verfügbar.")
            path = (
                f"/nodes/{quote(node)}/{kind}/{vmid}/rrddata"
                f"?timeframe={quote(timeframe)}&cf=AVERAGE"
            )
            rows = await self._proxmox_get(client, path, headers) or []

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

    async def _proxmox_authed_client(self) -> tuple[httpx.AsyncClient, dict[str, str]]:
        """Open an authenticated Proxmox HTTP client (caller must close)."""
        if not self.settings.proxmox_configured:
            raise RuntimeError("Proxmox ist nicht konfiguriert.")
        client = httpx.AsyncClient(
            verify=self.settings.proxmox_verify_ssl,
            timeout=httpx.Timeout(20.0, connect=8.0),
        )
        try:
            headers = self._proxmox_headers()
            if not headers and self.settings.proxmox_password:
                headers = await self._proxmox_ticket(client)
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

        client, headers = await self._proxmox_authed_client()
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

        client, headers = await self._proxmox_authed_client()
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

        client, headers = await self._proxmox_authed_client()
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

        client, headers = await self._proxmox_authed_client()
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
        client, headers = await self._proxmox_authed_client()
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
                    raise RuntimeError(
                        f"{'LXC' if kind == 'lxc' else 'VM'} {vmid} läuft bereits."
                    ) from exc
                if "not running" in low or "isn't running" in low:
                    raise RuntimeError(
                        f"{'LXC' if kind == 'lxc' else 'VM'} {vmid} ist nicht gestartet."
                    ) from exc
                raise RuntimeError(
                    f"Proxmox Power-{action} fehlgeschlagen "
                    f"(HTTP {exc.response.status_code})"
                    + (f": {detail}" if detail else ".")
                ) from exc
        finally:
            await client.aclose()

        kind_label = "LXC" if kind == "lxc" else "VM"
        return {
            "ok": True,
            "guest_id": guest_id,
            "action": action,
            "upid": upid,
            "message": f"{kind_label} {vmid} wird {labels[action]}…",
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
            labels = dict(c.labels or {})
            meta: dict[str, Any] = {"short_id": c.short_id}
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
        self, guests: list[TopologyEntity]
    ) -> tuple[list[tuple[str, str]], str | None]:
        """Return ((parent_id, ip) pairs, optional skip reason for topology.errors)."""
        candidates: list[tuple[str, str]] = []
        for g in guests:
            if g.status != EntityStatus.RUNNING or not g.ip_addresses:
                continue
            # Prefer first non-loopback IP
            candidates.append((g.id, g.ip_addresses[0]))

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
        self, targets: list[tuple[str, str]]
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

        async def one(parent_id: str, ip: str) -> list[TopologyEntity]:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self._discover_docker_ssh(parent_id, ip),
                        timeout=per_host,
                    )
                except asyncio.TimeoutError:
                    logger.debug("SSH Docker scan %s (%s): timed out after %.1fs", parent_id, ip, per_host)
                    return []
                except Exception as exc:
                    logger.debug("SSH Docker scan %s (%s): %s", parent_id, ip, exc)
                    return []

        tasks = [asyncio.create_task(one(pid, ip)) for pid, ip in targets]
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

    async def _discover_docker_ssh(self, parent_id: str, ip: str) -> list[TopologyEntity]:
        s = self.settings
        # Include Compose project/service labels for dashboard stack grouping.
        cmd = (
            "docker ps -a --format "
            "'{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|"
            "{{.Label \"com.docker.compose.project\"}}|"
            "{{.Label \"com.docker.compose.service\"}}'"
        )
        # Keep connect short; command gets the remaining per-host budget via wait_for.
        connect_timeout = min(2.0, s.docker_ssh_timeout)
        async with asyncssh.connect(
            ip,
            port=s.docker_ssh_port,
            username=s.docker_ssh_user,
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
            # Names can be comma-separated aliases — prefer the first.
            name = name.split(",", 1)[0].strip()
            status = EntityStatus.RUNNING if status_raw.lower().startswith("up") else EntityStatus.STOPPED
            version = image.split(":")[-1] if ":" in image else None
            labels: dict[str, str] = {}
            if compose_project:
                labels["com.docker.compose.project"] = compose_project
            if compose_service:
                labels["com.docker.compose.service"] = compose_service
            meta: dict[str, Any] = {"via": "ssh", "raw_status": status_raw}
            if compose_project:
                meta["compose_project"] = compose_project
            if compose_service:
                meta["compose_service"] = compose_service
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
