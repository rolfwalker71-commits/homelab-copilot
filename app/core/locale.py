"""German locale helpers for dates and timestamps.

All user-facing and persisted timestamps use:
  DD.MM.YYYY, HH:mm:ss Uhr
"""

from __future__ import annotations

from datetime import datetime, timezone
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
