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


def guest_kind(guest_id: str | None) -> str:
    """``lxc`` or ``qemu`` from a guest id; empty if not a Proxmox guest."""
    parts = str(guest_id or "").split(":")
    if len(parts) == 3 and parts[0] in {"lxc", "qemu"}:
        return parts[0]
    return ""


def is_current_marker(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    name = str(row.get("name") or "").strip()
    if name.lower() == "current":
        return True
    return bool(row.get("current"))


def can_rollback_snap(row: dict[str, Any] | None) -> bool:
    """Rollback targets a named snap — never the running-disk ``current`` marker."""
    if not isinstance(row, dict):
        return False
    name = str(row.get("name") or "").strip()
    if not name or is_current_marker(row):
        return False
    return True


def _snap_ts(row: dict[str, Any]) -> int:
    try:
        return int(row.get("snaptime") or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_parent(raw: Any) -> str:
    return str(raw or "").strip()


def _effective_parent(
    name: str,
    parent_of: dict[str, str],
    names: set[str],
) -> str:
    """Follow ``parent``; missing / self / cycle → empty (treat as Wurzel)."""
    parent = parent_of.get(name, "")
    if not parent or parent == name or parent not in names:
        return ""
    seen = {name}
    walk = parent
    while walk:
        if walk in seen:
            return ""
        seen.add(walk)
        nxt = parent_of.get(walk, "")
        if not nxt or nxt not in names or nxt == walk:
            break
        walk = nxt
    return parent


def build_snapshot_tree(
    snaps: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Flatten Proxmox snaps into a parent/child tree (DFS, depth, German roles).

    ``current`` is the running-disk marker (not a rollback target). Its
    ``parent`` is the active named snap. Cycles and missing parents become
    roots. Siblings sort by snaptime then name so the chain is obvious.
    """
    by_name: dict[str, dict[str, Any]] = {}
    for row in snaps or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name or name in by_name:
            continue
        by_name[name] = dict(row)
        by_name[name]["name"] = name

    if not by_name:
        return []

    names = set(by_name)
    raw_parent = {
        name: _normalize_parent(row.get("parent")) for name, row in by_name.items()
    }
    parent_of = {
        name: _effective_parent(name, raw_parent, names) for name in names
    }

    children: dict[str, list[str]] = {name: [] for name in names}
    roots: list[str] = []
    for name in names:
        parent = parent_of[name]
        if parent:
            children[parent].append(name)
        else:
            roots.append(name)

    def sibling_key(name: str) -> tuple[int, int, str]:
        # Named snaps first (oldest → newest); ``current`` last under its parent.
        marker = 1 if is_current_marker(by_name[name]) else 0
        return (marker, _snap_ts(by_name[name]), name)

    for kids in children.values():
        kids.sort(key=sibling_key)
    roots.sort(key=sibling_key)

    active = ""
    current_row = by_name.get("current")
    if current_row is not None:
        active = parent_of.get("current") or _normalize_parent(current_row.get("parent"))
        if active not in names:
            active = ""

    out: list[dict[str, Any]] = []
    visiting: set[str] = set()

    def walk(name: str, depth: int) -> None:
        if name in visiting:
            return
        visiting.add(name)
        row = dict(by_name[name])
        marker = is_current_marker(row)
        parent = parent_of.get(name, "")
        is_root = depth == 0 or not parent
        if marker:
            relation = "aktuell"
            relation_label = "Aktuell"
        elif is_root:
            relation = "wurzel"
            relation_label = "Wurzel"
        else:
            relation = "kind"
            relation_label = "Kind"
        row["parent"] = parent or None
        row["depth"] = depth
        row["is_root"] = is_root
        row["current"] = marker
        row["active"] = bool(active and name == active)
        row["can_rollback"] = can_rollback_snap(row)
        row["relation"] = relation
        row["relation_label"] = relation_label
        out.append(row)
        for child in children.get(name, []):
            walk(child, depth + 1)
        visiting.remove(name)

    for root in roots:
        walk(root, 0)
    return out
