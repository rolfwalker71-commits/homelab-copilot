"""German locale helpers for dates and timestamps.

All user-facing and persisted timestamps use:
  DD.MM.YYYY, HH:mm:ss Uhr
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")


def now_berlin() -> datetime:
    """Current time in Europe/Berlin."""
    return datetime.now(BERLIN)


def format_de(dt: datetime | None = None, *, with_uhr: bool = True) -> str:
    """Format datetime as German string: DD.MM.YYYY, HH:mm:ss [Uhr]."""
    if dt is None:
        dt = now_berlin()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(BERLIN)
    base = local.strftime("%d.%m.%Y, %H:%M:%S")
    return f"{base} Uhr" if with_uhr else base


def iso_utc(dt: datetime | None = None) -> str:
    """Machine-readable UTC ISO-8601 (for DB / APIs). Display via format_de()."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def format_bytes(value: Any, *, digits: int = 1) -> str:
    """Human-readable binary size (KiB/MiB/GiB), Proxmox-style."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    if n < 0:
        n = 0.0
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    if i == 0:
        return f"{int(n)} {units[i]}"
    return f"{n:.{digits}f} {units[i]}"


def format_uptime(seconds: Any) -> str:
    """Compact uptime: ``12d 4h``, ``3h 12m``, or ``45m``."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "—"
    if total < 0:
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def metric_level(pct: Any) -> str:
    """Traffic-light level for resource percentages."""
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return "unknown"
    if p >= 90:
        return "danger"
    if p >= 70:
        return "warn"
    return "ok"
