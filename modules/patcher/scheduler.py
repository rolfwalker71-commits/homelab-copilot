"""In-process daily / on-demand patch scans across all hosts."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from app.core.locale import BERLIN, format_de, iso_utc, now_berlin

logger = logging.getLogger(__name__)

_CRON_RE = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$")

ScanOneFn = Callable[..., Awaitable[dict[str, Any]]]
NotifyFn = Callable[[dict[str, Any]], Awaitable[None]]


def parse_daily_schedule(
    *,
    cron_expr: str = "",
    hour: int = 4,
) -> tuple[int, int]:
    """Return (minute, hour) for a daily schedule. Cron must be daily ``m h * * *``."""
    expr = " ".join((cron_expr or "").split())
    if expr:
        m = _CRON_RE.match(expr)
        if not m:
            raise ValueError(f"Ungültiger PATCHER_CRON: {cron_expr!r}")
        minute_s, hour_s, dom, mon, dow = m.groups()
        if dom != "*" or mon != "*" or dow != "*":
            logger.warning(
                "PATCHER_CRON %r ist nicht täglich — verwende Stunde/Minute, Dom/Mon/Dow ignoriert",
                cron_expr,
            )
        try:
            minute, hour_v = int(minute_s), int(hour_s)
        except ValueError as exc:
            raise ValueError(f"Ungültiger PATCHER_CRON: {cron_expr!r}") from exc
        if not (0 <= minute <= 59 and 0 <= hour_v <= 23):
            raise ValueError(f"Ungültiger PATCHER_CRON: {cron_expr!r}")
        return minute, hour_v
    return 0, max(0, min(23, int(hour)))


def seconds_until_next(minute: int, hour: int, *, now: datetime | None = None) -> float:
    now = now or now_berlin()
    if now.tzinfo is None:
        now = now.replace(tzinfo=BERLIN)
    else:
        now = now.astimezone(BERLIN)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


class ScanAllState:
    """Shared progress for sequential all-hosts scans (UI + daily job)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.running = False
        self.trigger = ""  # manual | daily
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.current_index = 0
        self.total = 0
        self.current_target: str | None = None
        self.current_target_id: str | None = None
        self.message = ""
        self.results: list[dict[str, Any]] = []
        self.last_summary: dict[str, Any] | None = None
        self.error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "trigger": self.trigger,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "current_index": self.current_index,
            "total": self.total,
            "current_target": self.current_target,
            "current_target_id": self.current_target_id,
            "message": self.message,
            "results": list(self.results),
            "last_summary": self.last_summary,
            "error": self.error,
            "percent": (
                int(100 * self.current_index / self.total)
                if self.total and not self.running
                else (
                    int(100 * max(0, self.current_index - 1) / self.total)
                    if self.total and self.running
                    else 0
                )
            ),
        }


SCAN_ALL = ScanAllState()


async def begin_scan_all(*, trigger: str = "manual") -> dict[str, Any] | None:
    """Claim the scan-all slot. Returns None if already running, else initial state."""
    async with SCAN_ALL._lock:
        if SCAN_ALL.running:
            return None
        SCAN_ALL.running = True
        SCAN_ALL.trigger = trigger
        SCAN_ALL.started_at = format_de(now_berlin())
        SCAN_ALL.finished_at = None
        SCAN_ALL.current_index = 0
        SCAN_ALL.total = 0
        SCAN_ALL.current_target = None
        SCAN_ALL.current_target_id = None
        SCAN_ALL.message = "Wird gestartet…"
        SCAN_ALL.results = []
        SCAN_ALL.last_summary = None
        SCAN_ALL.error = None
        return SCAN_ALL.to_dict()


async def run_scan_all_hosts(
    *,
    targets: list[Any],
    scan_one: ScanOneFn,
    trigger: str = "manual",
    on_complete: NotifyFn | None = None,
    already_begun: bool = False,
) -> dict[str, Any]:
    """Scan every target sequentially. ``scan_one(target)`` must return a result dict."""
    if not already_begun:
        begun = await begin_scan_all(trigger=trigger)
        if begun is None:
            return {"ok": False, "error": "Scan läuft bereits.", **SCAN_ALL.to_dict()}
    else:
        SCAN_ALL.trigger = trigger
        SCAN_ALL.message = "Starte Prüfung aller Hosts…"

    SCAN_ALL.total = len(targets)
    SCAN_ALL.message = "Starte Prüfung aller Hosts…"

    hosts_with_updates = 0
    hosts_with_errors = 0
    total_updates = 0
    total_security = 0

    try:
        if not targets:
            SCAN_ALL.message = "Keine Hosts zum Prüfen."
        for idx, target in enumerate(targets, start=1):
            tid = getattr(target, "id", None) or (
                target.get("id") if isinstance(target, dict) else str(target)
            )
            tname = getattr(target, "name", None) or (
                target.get("name") if isinstance(target, dict) else str(tid)
            )
            SCAN_ALL.current_index = idx
            SCAN_ALL.current_target = tname
            SCAN_ALL.current_target_id = tid
            SCAN_ALL.message = f"Prüfe {tname} ({idx}/{len(targets)})…"
            try:
                result = await scan_one(target)
                status = result.get("status") or "success"
                summary = result.get("summary") or {}
                pending = int(summary.get("total") or 0)
                security = int(summary.get("security") or 0)
                entry = {
                    "target_id": tid,
                    "target_name": tname,
                    "status": status,
                    "summary": summary,
                    "error": result.get("error"),
                    "scan_id": result.get("scan_id"),
                }
                SCAN_ALL.results.append(entry)
                if status == "failed":
                    hosts_with_errors += 1
                else:
                    total_updates += pending
                    total_security += security
                    if pending > 0:
                        hosts_with_updates += 1
            except Exception as exc:
                logger.exception("scan-all failed for %s", tid)
                hosts_with_errors += 1
                SCAN_ALL.results.append(
                    {
                        "target_id": tid,
                        "target_name": tname,
                        "status": "failed",
                        "summary": {},
                        "error": str(exc),
                    }
                )

        summary = {
            "hosts_scanned": len(targets),
            "hosts_with_updates": hosts_with_updates,
            "hosts_with_errors": hosts_with_errors,
            "total_updates": total_updates,
            "total_security": total_security,
            "trigger": trigger,
            "finished_at": format_de(now_berlin()),
            "finished_at_iso": iso_utc(),
        }
        SCAN_ALL.last_summary = summary
        SCAN_ALL.message = (
            f"Fertig: {hosts_with_updates} Host(s) mit Updates, "
            f"{hosts_with_errors} Fehler, {total_updates} Pakete "
            f"({total_security} Security)."
        )
        if on_complete:
            try:
                await on_complete(summary)
            except Exception:
                logger.exception("scan-all notify failed")
        return {"ok": True, **SCAN_ALL.to_dict()}
    except Exception as exc:
        SCAN_ALL.error = str(exc)
        SCAN_ALL.message = f"Abgebrochen: {exc}"
        logger.exception("scan-all aborted")
        return {"ok": False, "error": str(exc), **SCAN_ALL.to_dict()}
    finally:
        SCAN_ALL.finished_at = format_de(now_berlin())
        SCAN_ALL.running = False
        SCAN_ALL.current_target = None
        SCAN_ALL.current_target_id = None


async def daily_scan_loop(
    *,
    enabled: bool,
    cron_expr: str,
    hour: int,
    get_targets_and_scan: Callable[[], Awaitable[None]],
) -> None:
    """Sleep until next daily slot, then run get_targets_and_scan()."""
    if not enabled:
        logger.info("Patcher Daily-Scan deaktiviert")
        return
    try:
        minute, hour_v = parse_daily_schedule(cron_expr=cron_expr, hour=hour)
    except ValueError:
        logger.exception("Patcher Daily-Scan: ungültiger Zeitplan — Loop gestoppt")
        return

    logger.info(
        "Patcher Daily-Scan aktiv — täglich um %02d:%02d Europe/Berlin",
        hour_v,
        minute,
    )
    while True:
        wait_s = seconds_until_next(minute, hour_v)
        logger.info(
            "Nächster Patcher-Daily-Scan in %.0f s (%s)",
            wait_s,
            format_de(now_berlin()),
        )
        try:
            await asyncio.sleep(wait_s)
            await get_targets_and_scan()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Patcher Daily-Scan Fehler")
            await asyncio.sleep(60)
