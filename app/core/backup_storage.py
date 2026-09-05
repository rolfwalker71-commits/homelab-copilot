"""Copilot + Hetzner Storage Box KPI usage (statvfs / dest probe).

Copilot numbers come from the local filesystem of ``BACKUP_COPILOT_DIR``.
Hetzner quota is probed over dest SFTP STATVFS or SSH ``df`` and cached.
Never invent capacity — unknown quota stays unknown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any

from app.core.docker_control import DockerControlError

logger = logging.getLogger(__name__)

WARN_PCT = 90.0
HETZNER_CACHE_TTL_SEC = 300.0
_HETZNER_PROBE_TIMEOUT = 12.0

_SI_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")
_DF_COUNTS = re.compile(r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*%")

_hetzner_lock = asyncio.Lock()
_hetzner_cache: dict[str, Any] | None = None
_hetzner_cache_at = 0.0
_hetzner_cache_key = ""


def format_si_de(value: Any, *, digits: int = 1) -> str:
    """Human SI size with German decimal comma, e.g. ``12,4 GB``."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    if n < 0:
        n = 0.0
    i = 0
    while n >= 1000 and i < len(_SI_UNITS) - 1:
        n /= 1000
        i += 1
    if i == 0:
        return f"{int(n)} {_SI_UNITS[i]}"
    formatted = f"{n:.{digits}f}".replace(".", ",")
    return f"{formatted} {_SI_UNITS[i]}"


def clamp_percent(used: Any, total: Any) -> float | None:
    """Used/total as 0–100, or ``None`` when capacity is unknown."""
    try:
        u = float(used)
        t = float(total)
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None
    pct = 100.0 * u / t
    if pct < 0:
        return 0.0
    if pct > 100:
        return 100.0
    return pct


def usage_from_vfs(
    *,
    frsize: int,
    blocks: int,
    bavail: int,
    bfree: int | None = None,
) -> tuple[int, int, int] | None:
    """``(used, free, total)`` from statvfs fields. ``used = total - free``."""
    del bfree  # reserved blocks stay in used — operator cares how full the disk is
    try:
        fr = int(frsize)
        tot_blocks = int(blocks)
        avail = int(bavail)
    except (TypeError, ValueError):
        return None
    if fr <= 0 or tot_blocks <= 0:
        return None
    total = fr * tot_blocks
    free = max(0, fr * avail)
    used = max(0, total - free)
    return used, free, total


def parse_df_output(text: str, *, block_bytes: int = 1024) -> tuple[int, int, int] | None:
    """Parse ``df`` Size/Used/Avail/Use% (handles Storage Box wrapped lines)."""
    if not text or block_bytes <= 0:
        return None
    match = _DF_COUNTS.search(text)
    if not match:
        return None
    total_u, _used_u, avail_u, _pct = (int(x) for x in match.groups())
    if total_u <= 0:
        return None
    total = total_u * block_bytes
    free = max(0, avail_u * block_bytes)
    used = max(0, total - free)
    return used, free, total


def _empty_usage(
    *,
    configured: bool,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "configured": configured,
        "quota_known": False,
        "used_bytes": None,
        "free_bytes": None,
        "total_bytes": None,
        "used_pct": None,
        "used_label": "—",
        "free_label": "—",
        "total_label": "—",
        "warn": False,
        "message": message,
        "cached": False,
        "source": None,
    }
    out.update(extra)
    return out


def serialize_usage(
    used: int,
    free: int,
    total: int,
    *,
    source: str,
    **extra: Any,
) -> dict[str, Any]:
    pct = clamp_percent(used, total)
    out: dict[str, Any] = {
        "configured": True,
        "quota_known": True,
        "used_bytes": int(used),
        "free_bytes": int(free),
        "total_bytes": int(total),
        "used_pct": round(pct, 1) if pct is not None else None,
        "used_label": format_si_de(used),
        "free_label": format_si_de(free),
        "total_label": format_si_de(total),
        "warn": pct is not None and pct >= WARN_PCT,
        "message": None,
        "cached": False,
        "source": source,
    }
    out.update(extra)
    return out


def copilot_fs_usage() -> dict[str, Any]:
    """Filesystem fill of ``BACKUP_COPILOT_DIR`` / default ``DATA_DIR/backups``."""
    try:
        from backup_verifier.config import get_backup_settings

        path = get_backup_settings().copilot_dir
    except Exception:
        from app.config import get_settings

        path = Path(get_settings().data_dir) / "backups"
    extra = {"id": "copilot", "label": "Copilot", "path": str(path)}
    try:
        path.mkdir(parents=True, exist_ok=True)
        st = os.statvfs(path)
    except OSError as exc:
        logger.warning("Copilot-Speicher statvfs fehlgeschlagen (%s): %s", path, exc)
        return _empty_usage(
            configured=True,
            message="Quota unbekannt",
            **extra,
        )
    triple = usage_from_vfs(
        frsize=int(st.f_frsize or st.f_bsize or 0),
        blocks=int(st.f_blocks),
        bavail=int(st.f_bavail),
        bfree=int(st.f_bfree),
    )
    if triple is None:
        return _empty_usage(
            configured=True,
            message="Quota unbekannt",
            **extra,
        )
    used, free, total = triple
    return serialize_usage(used, free, total, source="statvfs", **extra)


def reset_hetzner_cache() -> None:
    """Test helper — drop the dest quota cache."""
    global _hetzner_cache, _hetzner_cache_at, _hetzner_cache_key
    _hetzner_cache = None
    _hetzner_cache_at = 0.0
    _hetzner_cache_key = ""


def _dest_cache_key(dest: dict[str, Any]) -> str:
    return "|".join(
        (
            str(dest.get("id") or ""),
            str(dest.get("host") or ""),
            str(dest.get("remote_path") or ""),
            str(dest.get("port") or ""),
        )
    )


def pick_hetzner_dest(rows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """First enabled Storage Box dest (preset or ``*.your-storagebox.de``)."""
    from backup_verifier.destinations import KIND_SFTP, is_hetzner_storagebox

    for row in rows or []:
        if not row.get("enabled"):
            continue
        if row.get("kind") != KIND_SFTP:
            continue
        host = str(row.get("host") or "").strip()
        if not host:
            continue
        if is_hetzner_storagebox(dest=row):
            return row
    return None


def _vfs_to_usage(vfs: dict[str, Any], *, source: str, **extra: Any) -> dict[str, Any] | None:
    triple = usage_from_vfs(
        frsize=int(vfs.get("frsize") or 0),
        blocks=int(vfs.get("blocks") or 0),
        bavail=int(vfs.get("bavail") or 0),
        bfree=int(vfs.get("bfree") or 0) if vfs.get("bfree") is not None else None,
    )
    if triple is None:
        return None
    used, free, total = triple
    return serialize_usage(used, free, total, source=source, **extra)


async def _probe_sftp_statvfs(
    dest: dict[str, Any],
    extra: dict[str, Any],
) -> dict[str, Any] | None:
    from app.config import get_settings
    from backup_verifier.destinations import dest_sftp_port, resolve_auth
    from backup_verifier import sshutil

    settings = get_settings()
    auth = resolve_auth(dest, settings)
    host = str(dest.get("host") or "").strip()
    remote = str(dest.get("remote_path") or "").strip() or "."
    port = dest_sftp_port(dest)
    vfs = await sshutil.sftp_statvfs(
        settings,
        host,
        remote,
        username=auth["username"],
        key=auth.get("key"),
        key_pem=auth.get("key_pem"),
        password=auth.get("password"),
        port=port,
        timeout=8.0,
    )
    return _vfs_to_usage(vfs, source="sftp_statvfs", **extra)


async def _probe_ssh_df(
    dest: dict[str, Any],
    extra: dict[str, Any],
) -> dict[str, Any] | None:
    from app.config import get_settings
    from backup_verifier.destinations import dest_rsync_ssh_port, resolve_auth
    from backup_verifier import sshutil

    settings = get_settings()
    auth = resolve_auth(dest, settings)
    host = str(dest.get("host") or "").strip()
    remote = str(dest.get("remote_path") or "").strip()
    port = dest_rsync_ssh_port(dest)
    run_kw = dict(
        username=auth["username"],
        key=auth.get("key"),
        key_pem=auth.get("key_pem"),
        password=auth.get("password"),
        port=port,
        timeout=8.0,
    )
    commands: list[tuple[str, int]] = []
    if remote and remote not in (".",):
        commands.append((f"df -k {shlex.quote(remote)}", 1024))
    commands.append(("df -k", 1024))
    commands.append(("df", 1024))
    last_err: str | None = None
    for cmd, block in commands:
        try:
            stdout = await sshutil.ssh_run_ok(settings, host, cmd, **run_kw)
        except DockerControlError as exc:
            last_err = exc.message
            continue
        parsed = parse_df_output(stdout, block_bytes=block)
        if parsed is None:
            last_err = "df ohne Quota"
            continue
        used, free, total = parsed
        return serialize_usage(used, free, total, source="ssh_df", **extra)
    if last_err:
        logger.info("Hetzner-df ohne Ergebnis: %s", last_err)
    return None


async def _probe_hetzner(dest: dict[str, Any]) -> dict[str, Any]:
    extra = {
        "id": dest.get("id"),
        "label": dest.get("label") or "Hetzner",
        "host": dest.get("host") or "",
        "path": dest.get("remote_path") or "",
    }
    try:
        hit = await _probe_sftp_statvfs(dest, extra)
        if hit:
            return hit
    except DockerControlError as exc:
        logger.info("Hetzner SFTP STATVFS: %s", exc.message)
    except Exception:
        logger.exception("Hetzner SFTP STATVFS fehlgeschlagen")
    try:
        hit = await _probe_ssh_df(dest, extra)
        if hit:
            return hit
    except DockerControlError as exc:
        logger.info("Hetzner SSH df: %s", exc.message)
    except Exception:
        logger.exception("Hetzner SSH df fehlgeschlagen")
    return _empty_usage(configured=True, message="Quota unbekannt", **extra)


async def hetzner_dest_usage(store: Any | None) -> dict[str, Any]:
    """Cached Storage Box quota, or empty state when no dest is configured."""
    global _hetzner_cache, _hetzner_cache_at, _hetzner_cache_key
    from backup_verifier.destinations import ensure_seeded

    if store is None:
        return _empty_usage(configured=False, message="Kein Ziel", id="hetzner", label="Hetzner")

    await ensure_seeded(store)
    rows = await store.list_destinations()
    dest = pick_hetzner_dest(rows)
    if dest is None:
        return _empty_usage(configured=False, message="Kein Ziel", id="hetzner", label="Hetzner")

    key = _dest_cache_key(dest)
    now = time.monotonic()
    async with _hetzner_lock:
        if (
            _hetzner_cache is not None
            and _hetzner_cache_key == key
            and (now - _hetzner_cache_at) < HETZNER_CACHE_TTL_SEC
        ):
            cached = dict(_hetzner_cache)
            cached["cached"] = True
            return cached
        try:
            payload = await asyncio.wait_for(_probe_hetzner(dest), timeout=_HETZNER_PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            payload = _empty_usage(
                configured=True,
                message="Quota unbekannt",
                id=dest.get("id"),
                label=dest.get("label") or "Hetzner",
                host=dest.get("host") or "",
                path=dest.get("remote_path") or "",
            )
        _hetzner_cache = dict(payload)
        _hetzner_cache_at = time.monotonic()
        _hetzner_cache_key = key
        out = dict(payload)
        out["cached"] = False
        return out


async def build_backup_storage(store: Any | None = None) -> dict[str, Any]:
    """Dashboard JSON: Copilot FS + Hetzner dest (cached)."""
    copilot = copilot_fs_usage()
    hetzner = await hetzner_dest_usage(store)
    return {"copilot": copilot, "hetzner": hetzner}


def bagel_dasharray(pct: float | None, *, circumference: float = 95.19) -> str:
    """SVG ``stroke-dasharray`` for a bagel ring (used vs rest)."""
    p = 0.0 if pct is None else float(pct)
    if p < 0:
        p = 0.0
    if p > 100:
        p = 100.0
    used = circumference * (p / 100.0)
    rest = max(0.0, circumference - used)
    return f"{used:.2f} {rest:.2f}"
