"""Live host/guest inventory for the ops-agent scope matrix."""

from __future__ import annotations

from typing import Any, Iterable

# Synthetic backup target — never a real inventory host (do not show as weggefallen).
COPILOT_DATA_ID = "copilot:data"


def _kind_value(ent: Any) -> str:
    raw = getattr(ent, "kind", None)
    if raw is None and isinstance(ent, dict):
        raw = ent.get("kind")
    if hasattr(raw, "value"):
        return str(raw.value)
    return str(raw or "").strip().lower()


def _status_value(ent: Any) -> str:
    raw = getattr(ent, "status", None)
    if raw is None and isinstance(ent, dict):
        raw = ent.get("status")
    if hasattr(raw, "value"):
        return str(raw.value)
    return str(raw or "").strip().lower()


def _ent_id(ent: Any) -> str:
    if isinstance(ent, dict):
        return str(ent.get("id") or "").strip()
    return str(getattr(ent, "id", "") or "").strip()


def _ent_name(ent: Any) -> str:
    if isinstance(ent, dict):
        return str(ent.get("name") or ent.get("id") or "").strip()
    return str(getattr(ent, "name", None) or getattr(ent, "id", "") or "").strip()


def _ent_node(ent: Any) -> str:
    if isinstance(ent, dict):
        return str(ent.get("node") or "").strip()
    return str(getattr(ent, "node", None) or "").strip()


def _row(
    *,
    target_id: str,
    name: str,
    kind: str,
    node: str = "",
    online: bool = False,
    present: bool = True,
) -> dict[str, Any]:
    kind_label = {
        "lxc": "LXC",
        "qemu": "VM",
        "manual": "Manuell",
        "host": "Host",
    }.get(kind, kind or "Host")
    return {
        "id": target_id,
        "target_id": target_id,
        "name": name or target_id,
        "kind": kind,
        "kind_label": kind_label,
        "node": node,
        "online": bool(online),
        "present": bool(present),
    }


def collect_live_hosts(
    snapshot: Any,
    manuals: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Guests (LXC/QEMU, also stopped), topology hosts, and patcher manuals."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    def _add(row: dict[str, Any]) -> None:
        tid = str(row.get("id") or "").strip()
        if not tid or tid in seen:
            return
        seen.add(tid)
        out.append(row)

    if snapshot is not None:
        for g in list(getattr(snapshot, "guests", None) or []):
            kind = _kind_value(g)
            if kind not in ("lxc", "qemu"):
                continue
            tid = _ent_id(g)
            if not tid:
                continue
            _add(
                _row(
                    target_id=tid,
                    name=_ent_name(g),
                    kind=kind,
                    node=_ent_node(g),
                    online=_status_value(g) == "running",
                    present=True,
                )
            )
        for h in list(getattr(snapshot, "hosts", None) or []):
            tid = _ent_id(h)
            if not tid:
                continue
            _add(
                _row(
                    target_id=tid,
                    name=_ent_name(h),
                    kind="host",
                    node=_ent_node(h),
                    online=_status_value(h) == "running",
                    present=True,
                )
            )

    for raw in manuals or []:
        if not isinstance(raw, dict):
            continue
        tid = str(raw.get("id") or "").strip()
        if not tid:
            continue
        _add(
            _row(
                target_id=tid,
                name=str(raw.get("name") or tid),
                kind=str(raw.get("kind") or "manual"),
                node=str(raw.get("node") or ""),
                online=True,
                present=True,
            )
        )

    out.sort(key=lambda r: (str(r.get("name") or "").lower(), str(r.get("id") or "")))
    return out


def overlay_local_scope(
    hosts: list[dict[str, Any]],
    *,
    local_patch: list[str] | None,
    local_image: list[str] | None,
    dirty: bool,
) -> list[dict[str, Any]]:
    """Keep unsaved Patchen/Images ticks when the board rebuilds from the server."""
    if not dirty:
        return [dict(h) for h in hosts]
    patch = {str(x).strip().lower() for x in (local_patch or []) if str(x).strip()}
    image = {str(x).strip().lower() for x in (local_image or []) if str(x).strip()}
    out: list[dict[str, Any]] = []
    for h in hosts:
        row = dict(h)
        tid = str(row.get("id") or row.get("target_id") or "").strip().lower()
        row["patch"] = tid in patch
        row["image"] = tid in image
        out.append(row)
    return out


def split_inventory_changes(
    *,
    live_ids: set[str],
    known_present_ids: set[str],
    known_gone_ids: set[str],
    pending_ids: set[str],
) -> tuple[set[str], set[str], set[str]]:
    """Return (appeared, disappeared, returned). Pending IDs are left untouched."""
    live = {str(x).strip() for x in live_ids if str(x).strip()}
    present = {str(x).strip() for x in known_present_ids if str(x).strip()}
    gone = {str(x).strip() for x in known_gone_ids if str(x).strip()}
    pending = {str(x).strip() for x in pending_ids if str(x).strip()}
    appeared = live - present - gone - pending
    disappeared = present - live - pending
    returned = gone & live
    return appeared, disappeared, returned


def is_synthetic_copilot_data(target_id: str) -> bool:
    return str(target_id or "").strip() == COPILOT_DATA_ID


def exclude_synthetic_ids(ids: Iterable[str]) -> set[str]:
    return {str(x).strip() for x in ids if str(x).strip() and not is_synthetic_copilot_data(str(x))}


def is_live_backup_target(
    target_id: str,
    *,
    live_ids: set[str],
    gone_ids: set[str] | None = None,
) -> bool:
    """True if this id is a present inventory host that may need a backup nag."""
    tid = str(target_id or "").strip()
    if not tid or is_synthetic_copilot_data(tid):
        return False
    gone = {str(x).strip() for x in (gone_ids or set()) if str(x).strip()}
    if tid in gone:
        return False
    return tid in {str(x).strip() for x in live_ids if str(x).strip()}


def host_row_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("target_id") or "").strip()


def belongs_in_host_matrix(row: dict[str, Any]) -> bool:
    """Ghost Copilot-/data rows stay off the host list (dedicated prompt only)."""
    return not is_synthetic_copilot_data(host_row_id(row))
