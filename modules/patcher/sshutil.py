"""SSH helpers for patch scan/apply (longer timeouts than discovery)."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from pathlib import Path

import asyncssh

from app.config import Settings, get_settings
from app.core.docker_control import DockerControlError, ssh_key_path

logger = logging.getLogger(__name__)

_SSH_KEEPALIVE_INTERVAL = 30
_SSH_KEEPALIVE_COUNT_MAX = 3


def _ssh_family(host: str) -> int:
    """Pin address family for literal IPs so we do not wait on the other stack."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return 0
    return socket.AF_INET if addr.version == 4 else socket.AF_INET6


def _connect_kwargs(
    settings: Settings,
    *,
    username: str | None = None,
    port: int | None = None,
    key: Path | None = None,
    connect_timeout: float = 15.0,
    host: str | None = None,
) -> dict:
    kwargs: dict = {
        "port": port or settings.docker_ssh_port,
        "username": username or settings.docker_ssh_user,
        "known_hosts": None,
        "connect_timeout": connect_timeout,
        "login_timeout": connect_timeout,
        "keepalive_interval": _SSH_KEEPALIVE_INTERVAL,
        "keepalive_count_max": _SSH_KEEPALIVE_COUNT_MAX,
        "client_keys": [str(key or ssh_key_path(settings))],
        # Same key as Docker discovery — do not wait on password/keyboard-interactive.
        "preferred_auth": ("publickey",),
    }
    if host:
        family = _ssh_family(host)
        if family:
            kwargs["family"] = family
    return kwargs


async def ssh_run(
    ip: str,
    cmd: str,
    *,
    timeout: float = 180.0,
    username: str | None = None,
    port: int | None = None,
    connect_timeout: float = 15.0,
    settings: Settings | None = None,
) -> tuple[str, str, int]:
    settings = settings or get_settings()
    try:
        async with asyncssh.connect(
            ip,
            **_connect_kwargs(
                settings,
                username=username,
                port=port,
                connect_timeout=connect_timeout,
                host=ip,
            ),
        ) as conn:
            try:
                result = await conn.run(cmd, check=False, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise DockerControlError(
                    f"SSH-Befehl-Timeout zu {ip} ({timeout:.0f}s) — "
                    f"Remote-Befehl hängt (apt-Sperre oder Spiegel-Server?).",
                    status_code=504,
                ) from exc
    except DockerControlError:
        raise
    except asyncio.TimeoutError as exc:
        raise DockerControlError(
            f"SSH-Verbindungs-Timeout zu {ip} ({connect_timeout:.0f}s) — "
            f"Host nicht erreichbar?",
            status_code=504,
        ) from exc
    except (OSError, asyncssh.Error) as exc:
        logger.warning("Patcher SSH %s: %s", ip, exc)
        raise DockerControlError(
            f"SSH zu {ip} fehlgeschlagen: {exc}",
            status_code=502,
        ) from exc

    stdout = result.stdout if isinstance(result.stdout, str) else (result.stdout or b"").decode(
        "utf-8", errors="replace"
    )
    stderr = result.stderr if isinstance(result.stderr, str) else (result.stderr or b"").decode(
        "utf-8", errors="replace"
    )
    return stdout, stderr, int(result.exit_status or 0)


async def ssh_run_ok(
    ip: str,
    cmd: str,
    *,
    timeout: float = 180.0,
    username: str | None = None,
    port: int | None = None,
    connect_timeout: float = 15.0,
    settings: Settings | None = None,
) -> str:
    stdout, stderr, code = await ssh_run(
        ip,
        cmd,
        timeout=timeout,
        username=username,
        port=port,
        connect_timeout=connect_timeout,
        settings=settings,
    )
    if code != 0:
        detail = (stderr or stdout or "").strip() or f"exit {code}"
        raise DockerControlError(
            f"Remote-Befehl fehlgeschlagen ({ip}): {detail}",
            status_code=502,
        )
    return stdout


async def ssh_probe(
    ip: str,
    *,
    username: str | None = None,
    port: int | None = None,
    connect_timeout: float = 15.0,
) -> dict:
    """Connectivity check: echo + uname."""
    stdout, stderr, code = await ssh_run(
        ip,
        "uname -a && echo OK",
        timeout=30.0,
        username=username,
        port=port,
        connect_timeout=connect_timeout,
    )
    ok = code == 0 and "OK" in stdout
    return {
        "ok": ok,
        "exit_code": code,
        "stdout": (stdout or "").strip()[:500],
        "stderr": (stderr or "").strip()[:300],
    }
