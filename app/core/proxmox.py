"""Proxmox API targets: one endpoint per independent host (not only a cluster)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


UNBOUND_MSG_TMPL = "Kein API-Zugang — Node ist kein Cluster-Mitglied von {via}"


class ProxmoxNodeUnboundError(RuntimeError):
    """Node is listed (or inferred) but has no API on the connected cluster."""

    def __init__(self, node: str, via: str, *, http_detail: str | None = None) -> None:
        self.node = node
        self.via = via
        self.http_detail = (http_detail or "").strip() or None
        super().__init__(unbound_message(via))


def unbound_message(via: str) -> str:
    label = (via or "").strip() or "dem verbundenen Host"
    return UNBOUND_MSG_TMPL.format(via=label)


@dataclass(frozen=True)
class ProxmoxEndpoint:
    """Credentials + base URL for one Proxmox API (cluster or standalone)."""

    id: str
    host: str
    port: int = 8006
    user: str = "root@pam"
    token_id: str = ""
    token_secret: str = ""
    password: str = ""
    verify_ssl: bool = False
    node_filter: str = ""

    @property
    def configured(self) -> bool:
        host = (self.host or "").strip()
        has_auth = bool(self.token_secret) or bool(self.password)
        return bool(host) and has_auth

    @property
    def base_url(self) -> str:
        return f"https://{(self.host or '').strip()}:{int(self.port or 8006)}/api2/json"

    def auth_headers(self) -> dict[str, str]:
        if self.token_id and self.token_secret:
            token_id = self.token_id
            if "!" not in token_id:
                token_id = f"{self.user}!{token_id}"
            return {"Authorization": f"PVEAPIToken={token_id}={self.token_secret}"}
        return {}

    def token_acl_subject(self) -> str:
        tid = self.token_id or "?"
        if "!" in tid:
            return tid
        return f"{self.user}!{tid}"


def endpoints_from_settings(settings: Any) -> list[ProxmoxEndpoint]:
    """Primary ``PROXMOX_*`` plus optional standalone ``PROXMOX_2_*``."""
    out: list[ProxmoxEndpoint] = []
    primary = ProxmoxEndpoint(
        id="primary",
        host=str(getattr(settings, "proxmox_host", "") or ""),
        port=int(getattr(settings, "proxmox_port", 8006) or 8006),
        user=str(getattr(settings, "proxmox_user", "root@pam") or "root@pam"),
        token_id=str(getattr(settings, "proxmox_token_id", "") or ""),
        token_secret=str(getattr(settings, "proxmox_token_secret", "") or ""),
        password=str(getattr(settings, "proxmox_password", "") or ""),
        verify_ssl=bool(getattr(settings, "proxmox_verify_ssl", False)),
        node_filter=str(getattr(settings, "proxmox_node", "") or ""),
    )
    if primary.configured:
        out.append(primary)

    extra = ProxmoxEndpoint(
        id="extra:2",
        host=str(getattr(settings, "proxmox_2_host", "") or ""),
        port=int(getattr(settings, "proxmox_2_port", 8006) or 8006),
        user=str(getattr(settings, "proxmox_2_user", "root@pam") or "root@pam"),
        token_id=str(getattr(settings, "proxmox_2_token_id", "") or ""),
        token_secret=str(getattr(settings, "proxmox_2_token_secret", "") or ""),
        password=str(getattr(settings, "proxmox_2_password", "") or ""),
        verify_ssl=bool(getattr(settings, "proxmox_2_verify_ssl", False)),
    )
    if extra.configured:
        same = out and (
            extra.host.strip() == out[0].host.strip()
            and int(extra.port) == int(out[0].port)
        )
        if not same:
            out.append(extra)
    return out


def strip_unbound_metrics(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Drop CPU/RAM/disk so the UI cannot show invented gauges."""
    out = dict(meta or {})
    for key in (
        "cpu",
        "maxcpu",
        "mem",
        "maxmem",
        "uptime",
        "disk",
        "maxdisk",
        "cpu_pct",
        "mem_pct",
        "disk_pct",
    ):
        out.pop(key, None)
    return out


def pve_response_detail(response: httpx.Response | None) -> str:
    if response is None:
        return ""
    try:
        body = response.json()
    except Exception:
        text = (response.text or "").strip()
        return text[:240]
    if isinstance(body, dict):
        for key in ("message", "errors", "data"):
            val = body.get(key)
            if val is None or val == "":
                continue
            text = str(val).strip()
            if text:
                return text[:240]
        return str(body)[:240]
    return str(body)[:240]


def format_proxmox_api_error(exc: BaseException) -> str:
    """German, status-aware message (HTTP 403/404, connection refused, …)."""
    if isinstance(exc, ProxmoxNodeUnboundError):
        msg = str(exc)
        if exc.http_detail:
            return f"{msg} ({exc.http_detail})"
        return msg

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code if exc.response is not None else 0
        labels = {
            401: "nicht autorisiert",
            403: "Zugriff verweigert",
            404: "nicht gefunden",
            500: "Serverfehler",
            502: "Bad Gateway",
            503: "nicht erreichbar",
        }
        head = f"HTTP {code}" if code else "HTTP-Fehler"
        hint = labels.get(code)
        if hint:
            head = f"{head} ({hint})"
        detail = pve_response_detail(exc.response)
        if detail:
            return f"{head} — {detail}"
        return head

    if isinstance(exc, httpx.ConnectError):
        raw = str(exc).strip() or type(exc).__name__
        low = raw.lower()
        if "refused" in low or "errno 111" in low or "111" in low:
            return "Verbindung abgelehnt (connection refused)"
        if "name or service not known" in low or "nodename nor servname" in low:
            return f"DNS/Host nicht auflösbar: {raw}"
        return f"Verbindung fehlgeschlagen: {raw}"

    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectTimeout, httpx.ReadTimeout)):
        return "Zeitüberschreitung bei der Proxmox-API"

    text = str(exc).strip()
    if text:
        return text
    return type(exc).__name__
