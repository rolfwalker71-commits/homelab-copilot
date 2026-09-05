"""Gap scan + non-overlapping backup slot planner (Europe/Berlin wall clock).

Missing = discovered Compose stacks (parent + project) that have **no**
schedule row yet. Existing enabled jobs occupy a window of ``interval``
minutes; new slots shift later by that interval until free.

Slots that pass midnight **roll to the next calendar day**. Cron keeps the
wrapped clock time (00:00 …) and the same daily/weekly preset — there is no
one-shot “next day only” job.
"""

from __future__ import annotations

from typing import Any

from backup_verifier.scheduler import cron_clock_hm, schedule_clock_hm

MINUTES_PER_DAY = 24 * 60
DEFAULT_INTERVAL = 10
DEFAULT_KEEP_LAST = 14
DEFAULT_KEEP_WEEKLY = 8
DEFAULT_FULL_EVERY_DAYS = 7
DEFAULT_START = "03:00"
DEFAULT_PRESET = "daily"
DEFAULT_ENGINE = "tar"


class PlanError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def parse_hhmm(value: str) -> int:
    """Minutes from midnight for ``HH:MM`` (Europe/Berlin wall clock)."""
    raw = (value or "").strip()
    if not raw:
        raise PlanError("Uhrzeit fehlt.")
    parts = raw.split(":")
    if len(parts) < 2:
        raise PlanError("Ungültige Uhrzeit — erwartet HH:MM.")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise PlanError("Ungültige Uhrzeit — erwartet HH:MM.") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise PlanError("Ungültige Uhrzeit — Stunde 0–23, Minute 0–59.")
    return hour * 60 + minute


def format_hhmm(minutes: int) -> str:
    minutes = int(minutes) % MINUTES_PER_DAY
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def stack_key(parent_id: str, stack: str) -> str:
    return f"{str(parent_id or '').strip()}::{str(stack or '').strip()}"


def windows_overlap(start_a: int, start_b: int, interval: int) -> bool:
    """True if two ``[start, start+interval)`` windows overlap on a 24h clock."""
    if interval <= 0:
        return (start_a % MINUTES_PER_DAY) == (start_b % MINUTES_PER_DAY)
    if interval >= MINUTES_PER_DAY:
        return True
    a = start_a % MINUTES_PER_DAY
    b = start_b % MINUTES_PER_DAY
    dist = min((a - b) % MINUTES_PER_DAY, (b - a) % MINUTES_PER_DAY)
    return dist < interval


def schedule_start_minutes(row: dict[str, Any]) -> int | None:
    """Parseable clock minute for a schedule, or None if the cron is too wide."""
    start = row.get("start_hm")
    if isinstance(start, str) and start.strip():
        try:
            return parse_hhmm(start)
        except PlanError:
            pass
    clock = schedule_clock_hm(row)
    if clock:
        return clock[0] * 60 + clock[1]
    clock = cron_clock_hm(str(row.get("cron_expr") or ""))
    if clock:
        return clock[0] * 60 + clock[1]
    return None


def existing_start_minutes(schedules: list[dict[str, Any]]) -> list[int]:
    """Clock minutes of **enabled** schedules that have a single start time."""
    out: list[int] = []
    for row in schedules:
        if not row.get("enabled", True):
            continue
        hm = schedule_start_minutes(row)
        if hm is not None:
            out.append(hm)
    return out


def classify_stacks(
    discovered: list[dict[str, Any]],
    schedules: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Split discovery into missing vs already scheduled (any schedule row)."""
    scheduled_keys: set[str] = set()
    scheduled: list[dict[str, Any]] = []
    for row in schedules:
        parent_id = str(row.get("parent_id") or "").strip()
        stack = str(row.get("stack") or "").strip()
        key = stack_key(parent_id, stack)
        if key == "::" or not parent_id or not stack:
            continue
        scheduled_keys.add(key)
        scheduled.append(dict(row))
    missing = [
        dict(s)
        for s in discovered
        if stack_key(str(s.get("parent_id") or ""), str(s.get("stack") or ""))
        not in scheduled_keys
    ]
    return {"missing": missing, "scheduled": scheduled}


def plan_slots(
    *,
    start_hm: str,
    interval_minutes: int = DEFAULT_INTERVAL,
    selected: list[dict[str, Any]],
    existing_starts: list[str] | list[int],
) -> list[dict[str, Any]]:
    """Assign each selected stack a non-overlapping start time.

    First candidate is ``start_hm``. If that window collides with an existing
    (or already planned) job, the slot is pushed later by ``interval_minutes``
    until free. Newly assigned times occupy the same window so two new jobs
    never share a minute.
    """
    interval = int(interval_minutes)
    if interval < 1 or interval > 180:
        raise PlanError("Abstand muss zwischen 1 und 180 Minuten liegen.")
    if not selected:
        raise PlanError("Keine Stacks ausgewählt.")

    occupied: list[int] = []
    for raw in existing_starts:
        if isinstance(raw, int):
            occupied.append(int(raw) % MINUTES_PER_DAY)
        else:
            occupied.append(parse_hhmm(str(raw)))

    start = parse_hhmm(start_hm)
    cursor = start
    out: list[dict[str, Any]] = []

    for item in selected:
        parent_id = str(item.get("parent_id") or "").strip()
        stack = str(item.get("stack") or item.get("project") or "").strip()
        if not parent_id or not stack:
            raise PlanError("Jeder Eintrag braucht parent_id und Stack.")
        skipped = 0
        while any(
            windows_overlap(cursor % MINUTES_PER_DAY, occ, interval)
            for occ in occupied
        ):
            cursor += interval
            skipped += 1
            if cursor - start > MINUTES_PER_DAY:
                raise PlanError(
                    "Kein freier Slot innerhalb von 24 Stunden — "
                    "Abstand verkleinern oder Start verschieben."
                )
        clock = cursor % MINUTES_PER_DAY
        out.append(
            {
                "parent_id": parent_id,
                "stack": stack,
                "guest_name": str(item.get("guest_name") or "").strip(),
                "guest_ip": str(item.get("guest_ip") or "").strip(),
                "start_hm": format_hhmm(clock),
                "start_minutes": clock,
                "wrapped": cursor >= MINUTES_PER_DAY,
                "shifted": skipped > 0,
                "skipped_slots": skipped,
            }
        )
        occupied.append(clock)
        cursor += interval

    return out
