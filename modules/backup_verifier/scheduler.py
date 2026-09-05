"""In-process backup schedules (Europe/Berlin). No host crontab required."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from app.core.locale import BERLIN, format_de, now_berlin

logger = logging.getLogger(__name__)

_CRON_RE = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$")

FireFn = Callable[[dict[str, Any]], Awaitable[None]]


def minute_key(dt: datetime | None = None) -> str:
    dt = dt or now_berlin()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BERLIN)
    else:
        dt = dt.astimezone(BERLIN)
    return dt.strftime("%Y-%m-%dT%H:%M")


def _expand_field(field: str, lo: int, hi: int) -> set[int]:
    field = (field or "").strip()
    if not field:
        raise ValueError("leeres Cron-Feld")
    values: set[int] = set()
    for raw in field.split(","):
        part = raw.strip()
        if not part:
            raise ValueError(f"ungültiges Cron-Feld: {field!r}")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step < 1:
                raise ValueError(f"ungültiger Cron-Schritt: {field!r}")
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        if start > end:
            start, end = end, start
        for v in range(start, end + 1, step):
            if lo <= v <= hi:
                values.add(v)
    if not values:
        raise ValueError(f"Cron-Feld außerhalb des Bereichs: {field!r}")
    return values


def cron_matches(expr: str, dt: datetime) -> bool:
    """True if 5-field cron ``m h dom mon dow`` matches ``dt`` (Europe/Berlin)."""
    expr = " ".join((expr or "").split())
    m = _CRON_RE.match(expr)
    if not m:
        return False
    minute_s, hour_s, dom_s, mon_s, dow_s = m.groups()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BERLIN)
    else:
        dt = dt.astimezone(BERLIN)
    try:
        minutes = _expand_field(minute_s, 0, 59)
        hours = _expand_field(hour_s, 0, 23)
        months = _expand_field(mon_s, 1, 12)
        days = _expand_field(dom_s, 1, 31)
        dows = _expand_field(dow_s, 0, 7)
    except ValueError:
        return False
    if 7 in dows:
        dows.add(0)
    if dt.minute not in minutes or dt.hour not in hours or dt.month not in months:
        return False
    cron_dow = (dt.weekday() + 1) % 7  # Sun=0 … Sat=6
    dom_star = dom_s == "*"
    dow_star = dow_s == "*"
    dom_ok = dt.day in days
    dow_ok = cron_dow in dows
    if not dom_star and not dow_star:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def next_run_after(expr: str, after: datetime | None = None) -> datetime | None:
    after = after or now_berlin()
    if after.tzinfo is None:
        after = after.replace(tzinfo=BERLIN)
    else:
        after = after.astimezone(BERLIN)
    expr = " ".join((expr or "").split())
    m = _CRON_RE.match(expr)
    if not m:
        return None
    minute_s, hour_s, _dom_s, mon_s, _dow_s = m.groups()
    try:
        minutes = sorted(_expand_field(minute_s, 0, 59))
        hours = sorted(_expand_field(hour_s, 0, 23))
        months = _expand_field(mon_s, 1, 12)
    except ValueError:
        return None
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for day_offset in range(0, 367):
        day = (start + timedelta(days=day_offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if day.month not in months:
            continue
        for hour in hours:
            for minute in minutes:
                candidate = day.replace(hour=hour, minute=minute)
                if candidate <= after:
                    continue
                if cron_matches(expr, candidate):
                    return candidate
    return None


def due_schedules(
    schedules: list[dict[str, Any]],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or now_berlin()
    key = minute_key(now)
    due: list[dict[str, Any]] = []
    for row in schedules:
        if not row.get("enabled"):
            continue
        expr = str(row.get("cron_expr") or "")
        if not cron_matches(expr, now):
            continue
        if str(row.get("last_fired_minute") or "") == key:
            continue
        due.append(row)
    return due


async def run_schedule_loop(
    store: Any,
    fire: FireFn,
    *,
    poll_seconds: float = 15.0,
    snapshot_ready: Callable[[], bool] | None = None,
) -> None:
    """Poll SQLite schedules and fire due backups (same minute, once)."""
    logger.info("backup_verifier In-App-Scheduler aktiv (Europe/Berlin)")
    while True:
        try:
            now = now_berlin()
            if snapshot_ready is not None and not snapshot_ready():
                logger.debug(
                    "backup scheduler: noch keine Topologie — %s",
                    format_de(now),
                )
            else:
                rows = await store.list_schedules()
                for row in due_schedules(rows, now):
                    sid = int(row["id"])
                    key = minute_key(now)
                    await store.mark_schedule_fired(sid, minute_key=key)
                    stack = row.get("stack") or "?"
                    logger.info(
                        "Geplantes Backup startet — %s (%s) %s",
                        stack,
                        row.get("parent_id"),
                        row.get("cron_expr"),
                    )
                    try:
                        await fire(row)
                    except Exception:
                        logger.exception(
                            "Geplantes Backup konnte nicht gestartet werden: %s",
                            stack,
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("backup scheduler tick fehlgeschlagen")
        await asyncio.sleep(max(5.0, float(poll_seconds)))
