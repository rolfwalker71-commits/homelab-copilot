"""Proxmox snapshot name + auto-retention helpers (pure)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.core.locale import BERLIN

AUTO_PREFIX = "hlops-"
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,39}$")
_RESERVED = frozenset({"current", "now", ".", ".."})


class SnapshotNameError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def validate_snap_name(name: str | None) -> str:
    raw = (name or "").strip()
    if not raw:
        raise SnapshotNameError("Snapshot-Name fehlt.")
    if raw.lower() in _RESERVED:
        raise SnapshotNameError(f"Name „{raw}“ ist reserviert.")
    if not _NAME_RE.match(raw):
        raise SnapshotNameError(
            "Snapshot-Name: Buchstabe zuerst, dann Buchstaben/Ziffern/_ . - "
            "(max. 40 Zeichen)."
        )
    return raw


def auto_snap_name(now: datetime | None = None, *, prefix: str = AUTO_PREFIX) -> str:
    dt = now or datetime.now(BERLIN)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BERLIN)
    else:
        dt = dt.astimezone(BERLIN)
    stamp = dt.strftime("%Y%m%d-%H%M%S")
    return validate_snap_name(f"{prefix}{stamp}")


def is_auto_snap(name: str | None, *, prefix: str = AUTO_PREFIX) -> bool:
    raw = (name or "").strip()
    if not raw or raw == "current":
        return False
    return raw.startswith(prefix)


def snaps_to_delete(
    snaps: list[dict[str, Any]] | None,
    *,
    keep: int,
    prefix: str = AUTO_PREFIX,
) -> list[str]:
    """Oldest auto snaps beyond ``keep`` (conservative; never touches ``current``)."""
    keep_n = max(0, int(keep))
    auto: list[tuple[int, str]] = []
    for row in snaps or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name or name == "current" or row.get("current"):
            continue
        if not is_auto_snap(name, prefix=prefix):
            continue
        try:
            ts = int(row.get("snaptime") or 0)
        except (TypeError, ValueError):
            ts = 0
        auto.append((ts, name))
    auto.sort(key=lambda t: (t[0], t[1]), reverse=True)
    if keep_n <= 0:
        return [name for _, name in auto]
    overflow = auto[keep_n:]
    return [name for _, name in overflow]


def clamp_keep(value: int | None, *, default: int = 3) -> int:
    try:
        n = int(value if value is not None else default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, 50))


def guest_can_snapshot(guest_id: str | None) -> bool:
    """True for Proxmox LXC/QEMU guests — never the node itself."""
    parts = str(guest_id or "").split(":")
    return len(parts) == 3 and parts[0] in {"lxc", "qemu"}
