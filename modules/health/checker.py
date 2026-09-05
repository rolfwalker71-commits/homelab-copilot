"""HTTP(S) reachability + TLS expiry (days left)."""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


def _parse_url(url: str) -> tuple[str, str, int, bool]:
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname or ""
    if not host:
        raise ValueError("URL ohne Host.")
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, host, port, scheme == "https"


def _cert_not_after(host: str, port: int, timeout: float) -> datetime | None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            der = ssock.getpeercert(binary_form=True)
    if not der:
        return None
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend

    cert = x509.load_der_x509_certificate(der, default_backend())
    expiry = getattr(cert, "not_valid_after_utc", None)
    if expiry is None:
        expiry = cert.not_valid_after.replace(tzinfo=timezone.utc)
    return expiry


async def check_url(url: str, *, timeout: float = 12.0) -> dict[str, Any]:
    """GET the URL; for HTTPS also read certificate expiry."""
    url = (url or "").strip()
    if not url:
        raise ValueError("URL fehlt.")
    scheme, host, port, is_https = _parse_url(url)
    result: dict[str, Any] = {
        "url": url,
        "status": "down",
        "http_code": None,
        "error": None,
        "cert_days_left": None,
        "cert_not_after": None,
    }

    if is_https:
        try:
            expiry = await asyncio.to_thread(_cert_not_after, host, port, timeout)
            if expiry is not None:
                days = int((expiry - datetime.now(timezone.utc)).total_seconds() // 86400)
                result["cert_days_left"] = days
                result["cert_not_after"] = expiry.date().isoformat()
        except Exception as exc:
            logger.debug("TLS-Probe %s:%s: %s", host, port, exc)

    try:
        async with httpx.AsyncClient(
            verify=False,
            timeout=httpx.Timeout(timeout, connect=min(6.0, timeout)),
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
        result["http_code"] = resp.status_code
        # Reachable service: 2xx/3xx and auth walls count as up.
        if resp.status_code < 500:
            result["status"] = "up"
        else:
            result["status"] = "down"
            result["error"] = f"HTTP {resp.status_code}"
    except Exception as exc:
        result["status"] = "down"
        result["error"] = str(exc).strip()[:240] or type(exc).__name__
    return result


def suggest_urls_from_topology(topology: dict[str, Any]) -> list[dict[str, str]]:
    """Cheap suggestions: published 80/443 and NPM-like containers."""
    guests = {g.get("id"): g for g in (topology.get("guests") or []) if isinstance(g, dict)}
    hosts = {h.get("id"): h for h in (topology.get("hosts") or []) if isinstance(h, dict)}
    parents = {**guests, **hosts}
    seen: set[str] = set()
    out: list[dict[str, str]] = []

    def add(url: str, label: str) -> None:
        url = url.rstrip("/")
        if url in seen:
            return
        seen.add(url)
        out.append({"url": url, "label": label})

    for c in topology.get("containers") or []:
        if not isinstance(c, dict):
            continue
        parent = parents.get(c.get("parent_id") or "")
        parent_ip = ""
        if parent:
            ips = parent.get("ip_addresses") or []
            parent_ip = ips[0] if ips else ""
        name = str(c.get("name") or "container")
        image = str(c.get("image") or "").lower()
        meta = c.get("meta") or {}
        ports = meta.get("published_ports") or []
        for p in ports:
            if not isinstance(p, dict):
                continue
            try:
                hport = int(p.get("host_port") or 0)
            except (TypeError, ValueError):
                continue
            proto = str(p.get("proto") or "tcp").lower()
            if proto not in {"tcp", ""}:
                continue
            hip = str(p.get("host_ip") or "0.0.0.0")
            if hip in {"127.0.0.1", "::1"}:
                continue
            bind = parent_ip
            if hip not in {"0.0.0.0", "::", "*", ""} and not hip.startswith("["):
                bind = hip
            if not bind:
                continue
            if hport in {443, 8443}:
                add(f"https://{bind}" if hport == 443 else f"https://{bind}:{hport}", name)
            elif hport in {80, 8080, 8000, 3000, 81}:
                add(f"http://{bind}" if hport == 80 else f"http://{bind}:{hport}", name)

        if "nginx-proxy-manager" in image or name.lower() in {"npm", "nginx-proxy-manager"}:
            if parent_ip:
                add(f"https://{parent_ip}", f"{name} (NPM)")

    return out[:80]
