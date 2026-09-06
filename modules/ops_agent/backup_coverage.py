"""Match backup_verifier schedules to guests / compose stacks.

Soll/Ist and missing-backup prompts must follow Compose-Stacks, not every
PVE guest. An active schedule covers that stack and the guest that runs it.
"""

from __future__ import annotations

from typing import Any


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _host_id(host: dict[str, Any]) -> str:
    return _norm(host.get("id") or host.get("target_id"))


def _host_name(host: dict[str, Any]) -> str:
    return _norm(
        host.get("name")
        or host.get("target_name")
        or host.get("guest_name")
        or host.get("hostname")
    )


def _stack_name(row: dict[str, Any]) -> str:
    return _norm(row.get("stack") or row.get("compose_project") or row.get("project"))


def is_active_schedule(row: dict[str, Any]) -> bool:
    if not _stack_name(row):
        return False
    return bool(row.get("enabled", True))


def schedule_covers_host(sched: dict[str, Any], host: dict[str, Any]) -> bool:
    """parent_id, guest hostname, or stack name matching the guest."""
    if not is_active_schedule(sched):
        return False
    hid = _host_id(host)
    hname = _host_name(host)
    pid = _norm(sched.get("parent_id"))
    gname = _norm(sched.get("guest_name"))
    stack = _stack_name(sched)
    if hid and pid and pid == hid:
        return True
    if hname and gname and gname == hname:
        return True
    if hname and stack and stack == hname:
        return True
    return False


def stack_belongs_to_host(stack: dict[str, Any], host: dict[str, Any]) -> bool:
    hid = _host_id(host)
    hname = _host_name(host)
    pid = _norm(stack.get("parent_id"))
    gname = _norm(stack.get("guest_name"))
    sname = _stack_name(stack)
    if hid and pid and pid == hid:
        return True
    if hname and gname and gname == hname:
        return True
    if hname and sname and sname == hname:
        return True
    return False


def schedule_covers_stack(sched: dict[str, Any], stack: dict[str, Any]) -> bool:
    if not is_active_schedule(sched):
        return False
    sp, ss = _norm(sched.get("parent_id")), _stack_name(sched)
    tp, ts = _norm(stack.get("parent_id")), _stack_name(stack)
    if ss and ts and ss == ts and sp and tp and sp == tp:
        return True
    sg, tg = _norm(sched.get("guest_name")), _norm(stack.get("guest_name"))
    if ss and ts and ss == ts and sg and tg and sg == tg:
        return True
    return False


def host_backupable_stacks(
    host: dict[str, Any], stacks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [s for s in stacks if stack_belongs_to_host(s, host)]


def host_needs_backup_nag(
    host: dict[str, Any],
    stacks: list[dict[str, Any]],
    schedules: list[dict[str, Any]],
) -> bool:
    """True only if this guest has compose stacks without an active schedule."""
    mine = host_backupable_stacks(host, stacks)
    if not mine:
        return False
    return not any(schedule_covers_host(s, host) for s in schedules)


def covered_host_keys(
    schedules: list[dict[str, Any]],
    hosts: list[dict[str, Any]] | None = None,
) -> set[str]:
    """Lowercased parent_id / guest name / stack keys that have an active plan."""
    keys: set[str] = set()
    for sched in schedules:
        if not is_active_schedule(sched):
            continue
        for raw in (
            sched.get("parent_id"),
            sched.get("guest_name"),
            sched.get("stack"),
        ):
            key = _norm(raw)
            if key:
                keys.add(key)
    for host in hosts or []:
        if any(schedule_covers_host(s, host) for s in schedules):
            hid = _host_id(host)
            if hid:
                keys.add(hid)
    return keys
