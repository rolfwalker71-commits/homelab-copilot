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


SETTINGS_HOST_ROWS_ATTR = "_proxmox_host_rows"


def endpoint_id_for_slot(slot: int) -> str:
    if int(slot) == 1:
        return "primary"
    return f"extra:{int(slot)}"


@dataclass(frozen=True)
class ProxmoxHostRow:
    """One persisted Proxmox API target (SQLite row or env bootstrap)."""

    slot: int
    host: str
    port: int = 8006
    user: str = "root@pam"
    token_id: str = ""
    token_secret: str = ""
    password: str = ""
    verify_ssl: bool = False
    label: str = ""

    @property
    def configured(self) -> bool:
        host = (self.host or "").strip()
        has_auth = bool(self.token_secret) or bool(self.password)
        return bool(host) and has_auth

    def to_endpoint(self) -> ProxmoxEndpoint:
        return ProxmoxEndpoint(
            id=endpoint_id_for_slot(self.slot),
            host=self.host,
            port=self.port,
            user=self.user,
            token_id=self.token_id,
            token_secret=self.token_secret,
            password=self.password,
            verify_ssl=self.verify_ssl,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": int(self.slot),
            "host": (self.host or "").strip(),
            "port": int(self.port or 8006),
            "user": (self.user or "root@pam").strip() or "root@pam",
            "token_id": (self.token_id or "").strip(),
            "token_secret": self.token_secret or "",
            "password": self.password or "",
            "verify_ssl": bool(self.verify_ssl),
            "label": (self.label or "").strip(),
        }


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


def host_row_from_dict(data: dict[str, Any]) -> ProxmoxHostRow:
    return ProxmoxHostRow(
        slot=int(data.get("slot") or 0),
        host=str(data.get("host") or "").strip(),
        port=int(data.get("port") or 8006),
        user=str(data.get("user") or "root@pam").strip() or "root@pam",
        token_id=str(data.get("token_id") or "").strip(),
        token_secret=str(data.get("token_secret") or ""),
        password=str(data.get("password") or ""),
        verify_ssl=bool(data.get("verify_ssl", False)),
        label=str(data.get("label") or "").strip(),
    )


def host_rows_from_dicts(rows: list[dict[str, Any]] | None) -> list[ProxmoxHostRow]:
    out: list[ProxmoxHostRow] = []
    for raw in rows or []:
        row = host_row_from_dict(raw)
        if (row.host or "").strip() and row.slot > 0:
            out.append(row)
    out.sort(key=lambda r: r.slot)
    return out


def _same_host_port(a: ProxmoxHostRow | None, b: ProxmoxHostRow | None) -> bool:
    if a is None or b is None:
        return False
    return a.host.strip() == b.host.strip() and int(a.port) == int(b.port)


def hosts_from_env(settings: Any) -> list[ProxmoxHostRow]:
    """Bootstrap rows from ``PROXMOX_*`` / ``PROXMOX_2_*`` (no SQLite yet)."""
    primary = ProxmoxHostRow(
        slot=1,
        host=str(getattr(settings, "proxmox_host", "") or ""),
        port=int(getattr(settings, "proxmox_port", 8006) or 8006),
        user=str(getattr(settings, "proxmox_user", "root@pam") or "root@pam"),
        token_id=str(getattr(settings, "proxmox_token_id", "") or ""),
        token_secret=str(getattr(settings, "proxmox_token_secret", "") or ""),
        password=str(getattr(settings, "proxmox_password", "") or ""),
        verify_ssl=bool(getattr(settings, "proxmox_verify_ssl", False)),
        label=str(getattr(settings, "proxmox_node", "") or ""),
    )
    extra = ProxmoxHostRow(
        slot=2,
        host=str(getattr(settings, "proxmox_2_host", "") or ""),
        port=int(getattr(settings, "proxmox_2_port", 8006) or 8006),
        user=str(getattr(settings, "proxmox_2_user", "root@pam") or "root@pam"),
        token_id=str(getattr(settings, "proxmox_2_token_id", "") or ""),
        token_secret=str(getattr(settings, "proxmox_2_token_secret", "") or ""),
        password=str(getattr(settings, "proxmox_2_password", "") or ""),
        verify_ssl=bool(getattr(settings, "proxmox_2_verify_ssl", False)),
        label="",
    )
    out: list[ProxmoxHostRow] = []
    if (primary.host or "").strip():
        out.append(primary)
    if (extra.host or "").strip() and not _same_host_port(
        out[0] if out else None, extra
    ):
        out.append(extra)
    return out


def merge_proxmox_hosts(
    db_rows: list[ProxmoxHostRow] | None, settings: Any
) -> list[ProxmoxHostRow]:
    """DB rows if present (after Setup save) else env bootstrap."""
    db = [r for r in (db_rows or []) if (r.host or "").strip()]
    if db:
        return db
    return hosts_from_env(settings)


def apply_host_rows_to_settings(settings: Any, rows: list[ProxmoxHostRow]) -> None:
    """Overlay Settings fields so Setup UI + discovery see the merged hosts."""

    def _set(key: str, value: Any) -> None:
        object.__setattr__(settings, key, value)

    _set("proxmox_host", "")
    _set("proxmox_port", 8006)
    _set("proxmox_user", "root@pam")
    _set("proxmox_token_id", "")
    _set("proxmox_token_secret", "")
    _set("proxmox_password", "")
    _set("proxmox_verify_ssl", False)
    _set("proxmox_2_host", "")
    _set("proxmox_2_port", 8006)
    _set("proxmox_2_user", "root@pam")
    _set("proxmox_2_token_id", "")
    _set("proxmox_2_token_secret", "")
    _set("proxmox_2_password", "")
    _set("proxmox_2_verify_ssl", False)

    for row in rows:
        if row.slot == 1:
            _set("proxmox_host", row.host)
            _set("proxmox_port", int(row.port or 8006))
            _set("proxmox_user", row.user or "root@pam")
            _set("proxmox_token_id", row.token_id)
            _set("proxmox_token_secret", row.token_secret)
            _set("proxmox_password", row.password)
            _set("proxmox_verify_ssl", bool(row.verify_ssl))
        elif row.slot == 2:
            _set("proxmox_2_host", row.host)
            _set("proxmox_2_port", int(row.port or 8006))
            _set("proxmox_2_user", row.user or "root@pam")
            _set("proxmox_2_token_id", row.token_id)
            _set("proxmox_2_token_secret", row.token_secret)
            _set("proxmox_2_password", row.password)
            _set("proxmox_2_verify_ssl", bool(row.verify_ssl))

    object.__setattr__(settings, SETTINGS_HOST_ROWS_ATTR, list(rows))


def _keep_secret(new: str | None, previous: str) -> str:
    return new if new else previous


def host_rows_from_setup_payload(
    payload: dict[str, Any],
    previous: list[ProxmoxHostRow],
    *,
    include_slot2: bool = True,
) -> list[ProxmoxHostRow]:
    """Build persisted rows from Setup POST. Blank secrets keep previous."""
    prev = {r.slot: r for r in previous}

    def _row(
        slot: int,
        host: str,
        port: Any,
        user: str,
        token_id: str,
        token_secret: str | None,
        password: str | None,
        verify_ssl: bool,
        label: str = "",
    ) -> ProxmoxHostRow | None:
        host = (host or "").strip()
        if not host:
            return None
        old = prev.get(slot)
        return ProxmoxHostRow(
            slot=slot,
            host=host,
            port=int(port or 8006),
            user=(user or "").strip() or "root@pam",
            token_id=(token_id or "").strip(),
            token_secret=_keep_secret(token_secret, old.token_secret if old else ""),
            password=_keep_secret(password, old.password if old else ""),
            verify_ssl=bool(verify_ssl),
            label=(label or (old.label if old else "")).strip(),
        )

    rows: list[ProxmoxHostRow] = []
    first = _row(
        1,
        str(payload.get("proxmox_host") or ""),
        payload.get("proxmox_port"),
        str(payload.get("proxmox_user") or ""),
        str(payload.get("proxmox_token_id") or ""),
        payload.get("proxmox_token_secret"),
        payload.get("proxmox_password"),
        bool(payload.get("proxmox_verify_ssl", False)),
    )
    if first:
        rows.append(first)

    if include_slot2:
        second = _row(
            2,
            str(payload.get("proxmox_2_host") or ""),
            payload.get("proxmox_2_port"),
            str(payload.get("proxmox_2_user") or ""),
            str(payload.get("proxmox_2_token_id") or ""),
            payload.get("proxmox_2_token_secret"),
            payload.get("proxmox_2_password"),
            bool(payload.get("proxmox_2_verify_ssl", False)),
        )
        if second and not _same_host_port(first, second):
            rows.append(second)
    elif 2 in prev and (prev[2].host or "").strip():
        if not _same_host_port(first, prev[2]):
            rows.append(prev[2])

    for extra in previous:
        if extra.slot >= 3 and (extra.host or "").strip():
            rows.append(extra)
    rows.sort(key=lambda r: r.slot)
    return rows


def endpoints_from_host_rows(rows: list[ProxmoxHostRow]) -> list[ProxmoxEndpoint]:
    seen: set[tuple[str, int]] = set()
    out: list[ProxmoxEndpoint] = []
    for row in rows:
        if not row.configured:
            continue
        key = (row.host.strip(), int(row.port or 8006))
        if key in seen:
            continue
        seen.add(key)
        out.append(row.to_endpoint())
    return out


def endpoints_from_settings(settings: Any) -> list[ProxmoxEndpoint]:
    """DB overlay on Settings if present, else ``PROXMOX_*`` / ``PROXMOX_2_*``."""
    overlay = getattr(settings, SETTINGS_HOST_ROWS_ATTR, None)
    if overlay:
        return endpoints_from_host_rows(list(overlay))
    return endpoints_from_host_rows(hosts_from_env(settings))


async def hydrate_proxmox_settings(settings: Any, store: Any) -> list[ProxmoxHostRow]:
    """Load SQLite hosts (if any) over env bootstrap onto the live Settings."""
    raw = await store.list_proxmox_hosts()
    merged = merge_proxmox_hosts(host_rows_from_dicts(raw), settings)
    apply_host_rows_to_settings(settings, merged)
    return merged


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
