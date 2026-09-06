"""Ops-Agent slot planner — 10-min raster, Europe/Berlin, no LLM.

Quiet hours for NEW windows: 20:00–23:50. That is evening after a typical
last backup batch and before the 04:00 daily scan / 05:00 restore-drill.
Existing ingested backup schedules keep their clock so tonight does not vanish.

Blocked clock ranges (not used for new proposals):
  03:50–04:20  daily patch scan (PATCHER_DAILY_HOUR default 4)
  04:50–05:20  restore-drill (BACKUP_DRILL_HOUR default 5)

Same guest/host: patch must not overlap backup, restore, or drill.
Backups stay on the global 10-min grid (existing planner convention).
Patches on different hosts still serialize (one apply at a time).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.core.locale import BERLIN
from backup_verifier.planner import (
    DEFAULT_INTERVAL,
    MINUTES_PER_DAY,
    format_hhmm,
    parse_hhmm,
    windows_overlap,
)
from ops_agent.policy import ConfirmPolicy, in_job_scope, needs_human

KIND_BACKUP = "backup"
KIND_PATCH = "patch"
KIND_DRILL = "drill"
KIND_RESTORE = "restore"

STATUS_ACCEPTED = "accepted"
STATUS_WAITING = "waiting_confirm"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

SOURCE_AGENT = "agent"
SOURCE_INGESTED = "ingested"
SOURCE_DRILL = "drill"

REASON_BACKUP_OVERRUN = "Backup läuft über — späteres Fenster verschoben."
REASON_PATCH_OVERRUN = "Patch läuft noch — späteres Fenster verschoben."
REASON_HOST_OFFLINE = "Host ist nicht online."
REASON_DISK = "Disk ist kritisch."
REASON_CONFLICT = "Konflikt mit Backup/Patch auf demselben Host."
REASON_RUNNING = "Backup, Restore oder Apply läuft bereits auf diesem Ziel."
REASON_NO_AUTO = "Host ist mit no-auto-patch markiert."
REASON_SNAP = "Snapshot nicht verfügbar — Patch-Fenster übersprungen."
REASON_OUT_OF_FOCUS = "Liegt außerhalb der Host-Auswahl."
REASON_HOST_GONE = "Host ist weggefallen — warte auf deine Entscheidung."
REASON_DRILL_BLOCK = "Restore-Drill um 05:00 — nicht in dieses Fenster legen."
REASON_SCAN_BLOCK = "Täglicher Scan um 04:00 — nicht in dieses Fenster legen."
REASON_BACKUP_CHAIN = "durch Agent: Anschluss an vorigen Auftrag"
REASON_DEST_FULL = "Ziel voll — Backup übersprungen durch Agent"
REASON_HOST_OFFLINE_CHAIN = "Host offline — Kette geht weiter, später erneut."
REASON_HUNG = "Auftrag hängt — bitte prüfen. Agent killt nichts."
REASON_REBOOT_WAIT = "Reboot nötig — wartet auf deine Bestätigung (Regel)."
REASON_REBOOT_NO_API = (
    "Reboot nötig, aber keine Gast-/Host-Reboot-API — bitte selbst."
)
REASON_REBOOT_DONE = "Reboot ausgelöst nach erfolgreichem Apply"
REASON_OFFLINE_TODAY = "{name} offline — Backup/Patch heute ausgelassen, fällt auf."
REASON_CAPACITY_WARN = "Ziel-Speicher wird nach der Kette knapp"
REASON_SMART_WARN = "SMART/Storage-Warnung — Backups laufen weiter"
REASON_EOL_PROPOSE = (
    "Release-Hop / EOL erkannt — nur Vorschlag, DistUpgrade startet der Agent nie."
)
REASON_PRUNE = "Ungenutzte Images bereinigt nach grüner Image-Welle"
REASON_COPILOT_DATA = "Copilot /data hat keinen Backup-Plan. So gewollt?"

DURATION_BACKUP = DEFAULT_INTERVAL
DURATION_PATCH_SECURITY = 20
DURATION_PATCH_REGULAR = 30
DURATION_DRILL = 20

DEFAULT_QUIET_START = "20:00"
DEFAULT_QUIET_END = "23:50"
DEFAULT_SCAN_HOUR = 4
DEFAULT_DRILL_HOUR = 5


@dataclass
class Need:
    kind: str
    target_id: str
    target_name: str
    stack: str = ""
    bucket: str = ""
    duration_min: int = DURATION_BACKUP
    packages: list[str] = field(default_factory=list)
    confirm_reasons: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    known_host: bool = True
    has_existing_schedule: bool = False
    engine: str = "tar"
    schedule_id: int | None = None
    source: str = SOURCE_AGENT


@dataclass
class Occupied:
    target_id: str
    kind: str
    start_min: int
    duration_min: int
    global_backup: bool = False
    label: str = ""


@dataclass
class PlannedWindow:
    kind: str
    target_id: str
    target_name: str
    stack: str = ""
    bucket: str = ""
    start_min: int = 0
    start_hm: str = ""
    duration_min: int = DURATION_BACKUP
    status: str = STATUS_ACCEPTED
    source: str = SOURCE_AGENT
    schedule_id: int | None = None
    needs_confirm: bool = False
    confirm_reasons: list[str] = field(default_factory=list)
    gates: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    reason: str = ""
    engine: str = "tar"
    tags: list[str] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target_id": self.target_id,
            "target_name": self.target_name,
            "stack": self.stack,
            "bucket": self.bucket,
            "start_min": self.start_min,
            "start_hm": self.start_hm,
            "duration_min": self.duration_min,
            "status": self.status,
            "source": self.source,
            "schedule_id": self.schedule_id,
            "needs_confirm": self.needs_confirm,
            "confirm_reasons": list(self.confirm_reasons),
            "gates": list(self.gates),
            "packages": list(self.packages),
            "reason": self.reason,
            "engine": self.engine,
            "tags": list(self.tags),
        }


def duration_for(*, kind: str, bucket: str = "") -> int:
    if kind == KIND_BACKUP:
        return DURATION_BACKUP
    if kind == KIND_DRILL:
        return DURATION_DRILL
    if bucket == "security":
        return DURATION_PATCH_SECURITY
    return DURATION_PATCH_REGULAR


def day_start(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=BERLIN)
    else:
        now = now.astimezone(BERLIN)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def snap_10_minutes(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BERLIN)
    else:
        dt = dt.astimezone(BERLIN)
    minute = (dt.minute // 10) * 10
    return dt.replace(minute=minute, second=0, microsecond=0)


def abs_minutes(dt: datetime, origin: datetime) -> int:
    origin = day_start(origin)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=origin.tzinfo)
    delta = dt - origin
    return int(delta.total_seconds() // 60)


def dt_from_abs(origin: datetime, minutes: int) -> datetime:
    return day_start(origin) + timedelta(minutes=int(minutes))


def clock_of(abs_min: int) -> int:
    return int(abs_min) % MINUTES_PER_DAY


def is_forbidden_clock(
    abs_min: int,
    *,
    scan_hour: int = DEFAULT_SCAN_HOUR,
    drill_hour: int = DEFAULT_DRILL_HOUR,
) -> str | None:
    """Return a German reason if this clock sits in scan or drill padding."""
    clock = clock_of(abs_min)
    scan_lo, scan_hi = scan_hour * 60 - 10, scan_hour * 60 + 20
    drill_lo, drill_hi = drill_hour * 60 - 10, drill_hour * 60 + 20
    if scan_lo <= clock < scan_hi:
        return REASON_SCAN_BLOCK
    if drill_lo <= clock < drill_hi:
        return REASON_DRILL_BLOCK
    return None


def in_quiet_hours(
    abs_min: int,
    *,
    quiet_start: str = DEFAULT_QUIET_START,
    quiet_end: str = DEFAULT_QUIET_END,
) -> bool:
    clock = clock_of(abs_min)
    lo = parse_hhmm(quiet_start)
    hi = parse_hhmm(quiet_end)
    if lo <= hi:
        return lo <= clock <= hi
    return clock >= lo or clock <= hi


def ranges_overlap(a0: int, a_dur: int, b0: int, b_dur: int) -> bool:
    return a0 < (b0 + b_dur) and b0 < (a0 + a_dur)


def kinds_conflict(a: str, b: str) -> bool:
    pair = {str(a), str(b)}
    if KIND_PATCH in pair and pair & {KIND_BACKUP, KIND_RESTORE, KIND_DRILL}:
        return True
    if KIND_BACKUP in pair and pair & {KIND_PATCH, KIND_RESTORE, KIND_DRILL}:
        return True
    if KIND_RESTORE in pair and pair & {KIND_PATCH, KIND_BACKUP}:
        return True
    return False


def slot_conflicts(
    *,
    target_id: str,
    kind: str,
    start_min: int,
    duration_min: int,
    occupied: list[Occupied],
) -> str | None:
    for occ in occupied:
        if not ranges_overlap(start_min, duration_min, occ.start_min, occ.duration_min):
            if kind in (KIND_BACKUP, KIND_DRILL) and occ.global_backup:
                # Global 10-min backup grid (clock), same as backup planner.
                if windows_overlap(
                    clock_of(start_min),
                    clock_of(occ.start_min),
                    DEFAULT_INTERVAL,
                ) and abs(start_min - occ.start_min) < MINUTES_PER_DAY:
                    return REASON_CONFLICT
            continue
        if occ.target_id == target_id and kinds_conflict(kind, occ.kind):
            return REASON_CONFLICT
        if kind == KIND_PATCH and occ.kind == KIND_PATCH:
            return REASON_CONFLICT
        if (
            kind in (KIND_BACKUP, KIND_DRILL)
            and occ.global_backup
            and windows_overlap(
                clock_of(start_min), clock_of(occ.start_min), DEFAULT_INTERVAL
            )
        ):
            return REASON_CONFLICT
    return None


def preferred_start_min(
    occupied: list[Occupied],
    now: datetime,
    *,
    quiet_start: str = DEFAULT_QUIET_START,
) -> int:
    """Next quiet-hour slot, after the last evening backup batch if there is one."""
    origin = day_start(now)
    now_min = abs_minutes(snap_10_minutes(now + timedelta(minutes=9)), origin)
    quiet = parse_hhmm(quiet_start)
    today_quiet = quiet
    if now_min > today_quiet:
        # Still today if we are inside evening hours; else tomorrow 20:00.
        if in_quiet_hours(now_min, quiet_start=quiet_start):
            cursor = now_min
        else:
            cursor = MINUTES_PER_DAY + quiet
    else:
        cursor = today_quiet

    last_evening = None
    for occ in occupied:
        if occ.kind != KIND_BACKUP:
            continue
        clock = clock_of(occ.start_min)
        if clock >= quiet:
            end = occ.start_min + occ.duration_min
            if last_evening is None or end > last_evening:
                last_evening = end
    if last_evening is not None and last_evening > cursor:
        cursor = last_evening
        if cursor % 10:
            cursor += 10 - (cursor % 10)
    return cursor


def next_free_slot(
    *,
    target_id: str,
    kind: str,
    duration_min: int,
    occupied: list[Occupied],
    start_min: int,
    scan_hour: int = DEFAULT_SCAN_HOUR,
    drill_hour: int = DEFAULT_DRILL_HOUR,
    quiet_start: str = DEFAULT_QUIET_START,
    quiet_end: str = DEFAULT_QUIET_END,
    horizon_min: int = MINUTES_PER_DAY * 3,
    require_quiet: bool = True,
    grid_min: int = 10,
) -> int:
    cursor = int(start_min)
    step = max(1, int(grid_min))
    if step > 1 and cursor % step:
        cursor += step - (cursor % step)
    limit = start_min + horizon_min
    while cursor <= limit:
        blocked = is_forbidden_clock(
            cursor, scan_hour=scan_hour, drill_hour=drill_hour
        )
        if blocked:
            cursor += step
            continue
        if require_quiet and not in_quiet_hours(
            cursor, quiet_start=quiet_start, quiet_end=quiet_end
        ):
            # Jump to next evening quiet start.
            day = cursor // MINUTES_PER_DAY
            clock = clock_of(cursor)
            q = parse_hhmm(quiet_start)
            if clock < q:
                cursor = day * MINUTES_PER_DAY + q
            else:
                cursor = (day + 1) * MINUTES_PER_DAY + q
            continue
        why = slot_conflicts(
            target_id=target_id,
            kind=kind,
            start_min=cursor,
            duration_min=duration_min,
            occupied=occupied,
        )
        if why is None:
            return cursor
        cursor += step
    raise RuntimeError("Kein freier Slot in den nächsten 72 Stunden.")


def ingest_schedule_windows(
    schedules: list[dict[str, Any]],
    *,
    now: datetime,
    drill_enabled: bool = True,
    drill_hour: int = DEFAULT_DRILL_HOUR,
) -> list[PlannedWindow]:
    """Turn saved backup schedules (+ optional drill) into board windows."""
    origin = day_start(now)
    now_min = abs_minutes(now, origin)
    out: list[PlannedWindow] = []
    for row in schedules:
        parent_id = str(row.get("parent_id") or "").strip()
        stack = str(row.get("stack") or "").strip()
        if not parent_id or not stack:
            continue
        start_hm = str(row.get("start_hm") or "").strip()
        if not start_hm:
            iso = str(row.get("next_run_iso") or "")
            if iso:
                try:
                    nxt = datetime.fromisoformat(iso)
                    if nxt.tzinfo is None:
                        nxt = nxt.replace(tzinfo=BERLIN)
                    start_hm = nxt.astimezone(BERLIN).strftime("%H:%M")
                except ValueError:
                    start_hm = ""
        if not start_hm:
            continue
        clock = parse_hhmm(start_hm)
        start_min = clock
        if start_min + 1 <= now_min:
            start_min += MINUTES_PER_DAY
        sid = row.get("id")
        try:
            schedule_id = int(sid) if sid is not None else None
        except (TypeError, ValueError):
            schedule_id = None
        enabled = bool(row.get("enabled", True))
        out.append(
            PlannedWindow(
                kind=KIND_BACKUP,
                target_id=parent_id,
                target_name=str(row.get("guest_name") or parent_id),
                stack=stack,
                bucket=KIND_BACKUP,
                start_min=start_min,
                start_hm=format_hhmm(clock),
                duration_min=DURATION_BACKUP,
                status=STATUS_ACCEPTED if enabled else STATUS_SKIPPED,
                source=SOURCE_INGESTED,
                schedule_id=schedule_id,
                reason="Übernommen aus dem bestehenden Backup-Zeitplan.",
                engine=str(row.get("engine") or "tar"),
            )
        )
    if drill_enabled:
        clock = drill_hour * 60
        start_min = clock
        if start_min + 1 <= now_min:
            start_min += MINUTES_PER_DAY
        out.append(
            PlannedWindow(
                kind=KIND_DRILL,
                target_id="*",
                target_name="Restore-Drill",
                stack="",
                bucket=KIND_DRILL,
                start_min=start_min,
                start_hm=format_hhmm(clock),
                duration_min=DURATION_DRILL,
                status=STATUS_ACCEPTED,
                source=SOURCE_DRILL,
                reason="Täglicher Restore-Drill (bereits geplant, Agent startet ihn nicht neu).",
            )
        )
    return out


def occupied_from_windows(windows: list[PlannedWindow]) -> list[Occupied]:
    out: list[Occupied] = []
    for w in windows:
        if w.status in (STATUS_SKIPPED, STATUS_DONE, STATUS_FAILED):
            continue
        out.append(
            Occupied(
                target_id=w.target_id,
                kind=w.kind,
                start_min=w.start_min,
                duration_min=w.duration_min,
                global_backup=w.kind in (KIND_BACKUP, KIND_DRILL),
                label=w.stack or w.target_name,
            )
        )
    return out


def evaluate_need_gates(
    need: Need,
    *,
    online: bool = True,
    disk_critical: bool = False,
    backup_running: bool = False,
    apply_running: bool = False,
    snap_unavailable: bool = False,
) -> list[str]:
    gates: list[str] = []
    if not online:
        gates.append(REASON_HOST_OFFLINE)
    if disk_critical:
        gates.append(REASON_DISK)
    if backup_running or apply_running:
        gates.append(REASON_RUNNING)
    if need.kind == KIND_PATCH and snap_unavailable:
        gates.append(REASON_SNAP)
    if "no-auto-patch" in need.confirm_reasons and need.kind == KIND_PATCH:
        # Tag is a confirm reason, not a skip — listed separately in policy.
        pass
    return gates


def propose_windows(
    needs: list[Need],
    occupied: list[Occupied],
    *,
    now: datetime,
    policy: ConfirmPolicy,
    quiet_start: str = DEFAULT_QUIET_START,
    quiet_end: str = DEFAULT_QUIET_END,
    scan_hour: int = DEFAULT_SCAN_HOUR,
    drill_hour: int = DEFAULT_DRILL_HOUR,
    host_online: dict[str, bool] | None = None,
    disk_critical: dict[str, bool] | None = None,
    running_targets: set[str] | None = None,
    snap_unavailable: set[str] | None = None,
    gone_ids: set[str] | None = None,
) -> tuple[list[PlannedWindow], list[PlannedWindow]]:
    """Assign non-overlapping slots. Returns (planned, skipped)."""
    online_map = host_online or {}
    disk_map = disk_critical or {}
    running = running_targets or set()
    no_snap = snap_unavailable or set()
    gone = {str(x).strip() for x in (gone_ids or set()) if str(x).strip()}
    live = list(occupied)
    planned: list[PlannedWindow] = []
    skipped: list[PlannedWindow] = []
    cursor = preferred_start_min(live, now, quiet_start=quiet_start)

    for need in needs:
        if need.target_id in gone:
            skipped.append(
                PlannedWindow(
                    kind=need.kind,
                    target_id=need.target_id,
                    target_name=need.target_name,
                    stack=need.stack,
                    bucket=need.bucket or need.kind,
                    status=STATUS_SKIPPED,
                    source=need.source,
                    reason=REASON_HOST_GONE,
                    tags=need.tags,
                )
            )
            continue
        if not in_job_scope(
            policy,
            kind=need.kind,
            bucket=need.bucket,
            target_id=need.target_id,
            gone_ids=gone,
        ):
            skipped.append(
                PlannedWindow(
                    kind=need.kind,
                    target_id=need.target_id,
                    target_name=need.target_name,
                    stack=need.stack,
                    bucket=need.bucket or need.kind,
                    status=STATUS_SKIPPED,
                    source=need.source,
                    reason=REASON_OUT_OF_FOCUS,
                    tags=need.tags,
                )
            )
            continue
        gates = evaluate_need_gates(
            need,
            online=online_map.get(need.target_id, True),
            disk_critical=disk_map.get(need.target_id, False),
            backup_running=need.target_id in running,
            apply_running=need.target_id in running,
            snap_unavailable=need.target_id in no_snap,
        )
        skip_reasons = [g for g in gates if g in (REASON_HOST_OFFLINE, REASON_DISK, REASON_SNAP)]
        if skip_reasons:
            skipped.append(
                PlannedWindow(
                    kind=need.kind,
                    target_id=need.target_id,
                    target_name=need.target_name,
                    stack=need.stack,
                    bucket=need.bucket or need.kind,
                    status=STATUS_SKIPPED,
                    source=need.source,
                    gates=gates,
                    reason=skip_reasons[0],
                    packages=need.packages,
                    tags=need.tags,
                )
            )
            continue

        wait, wait_reasons = needs_human(
            policy,
            kind=need.kind,
            bucket=need.bucket,
            confirm_reasons=need.confirm_reasons,
            tags=need.tags,
            has_existing_schedule=need.has_existing_schedule,
            known_host=need.known_host,
        )
        try:
            start_min = next_free_slot(
                target_id=need.target_id,
                kind=need.kind,
                duration_min=need.duration_min,
                occupied=live,
                start_min=cursor,
                scan_hour=scan_hour,
                drill_hour=drill_hour,
                quiet_start=quiet_start,
                quiet_end=quiet_end,
            )
        except RuntimeError as exc:
            skipped.append(
                PlannedWindow(
                    kind=need.kind,
                    target_id=need.target_id,
                    target_name=need.target_name,
                    stack=need.stack,
                    status=STATUS_SKIPPED,
                    reason=str(exc),
                    tags=need.tags,
                )
            )
            continue

        status = STATUS_WAITING if wait else STATUS_ACCEPTED
        reason = (
            "Wartet auf dich: " + ", ".join(_reason_de(r) for r in wait_reasons) + "."
            if wait
            else "Agent legt das Fenster selbst und startet zur geplanten Zeit."
        )
        win = PlannedWindow(
            kind=need.kind,
            target_id=need.target_id,
            target_name=need.target_name,
            stack=need.stack,
            bucket=need.bucket or need.kind,
            start_min=start_min,
            start_hm=format_hhmm(clock_of(start_min)),
            duration_min=need.duration_min,
            status=status,
            source=need.source,
            schedule_id=need.schedule_id,
            needs_confirm=wait,
            confirm_reasons=wait_reasons or list(need.confirm_reasons),
            gates=gates,
            packages=need.packages,
            reason=reason,
            engine=need.engine,
            tags=need.tags,
        )
        planned.append(win)
        live.append(
            Occupied(
                target_id=win.target_id,
                kind=win.kind,
                start_min=win.start_min,
                duration_min=win.duration_min,
                global_backup=win.kind in (KIND_BACKUP, KIND_DRILL),
                label=win.stack or win.target_name,
            )
        )
        cursor = start_min + need.duration_min
        if cursor % 10:
            cursor += 10 - (cursor % 10)
    return planned, skipped


def _reason_de(code: str) -> str:
    return {
        "kernel-docker": "Kernel- oder Docker-Patches",
        "images": "Image-Updates",
        "no-auto-patch": "no-auto-patch",
        "erstes-backup": "erstes Backup auf neuem Gast",
        "production": "Produktions-Host",
        "neuer-host": "noch nicht bekannter Host",
        "hard-stop": "harter Stopp",
    }.get(code, code)


def shift_later_window(
    window: PlannedWindow,
    occupied: list[Occupied],
    *,
    now: datetime,
    running_end_min: int,
    reason: str,
    scan_hour: int = DEFAULT_SCAN_HOUR,
    drill_hour: int = DEFAULT_DRILL_HOUR,
    quiet_start: str = DEFAULT_QUIET_START,
    quiet_end: str = DEFAULT_QUIET_END,
) -> tuple[PlannedWindow, dict[str, Any]]:
    """Move a later window past an overrun. Returns (updated, shift record)."""
    origin = day_start(now)
    old_min = window.start_min
    old_hm = window.start_hm or format_hhmm(clock_of(old_min))
    others = [
        o
        for o in occupied
        if not (
            o.target_id == window.target_id
            and o.start_min == window.start_min
            and o.kind == window.kind
        )
    ]
    start = max(running_end_min, abs_minutes(snap_10_minutes(now), origin))
    if start % 10:
        start += 10 - (start % 10)
    new_min = next_free_slot(
        target_id=window.target_id,
        kind=window.kind,
        duration_min=window.duration_min,
        occupied=others,
        start_min=start,
        scan_hour=scan_hour,
        drill_hour=drill_hour,
        quiet_start=quiet_start,
        quiet_end=quiet_end,
        require_quiet=False,
    )
    window.start_min = new_min
    window.start_hm = format_hhmm(clock_of(new_min))
    window.reason = reason
    shift = {
        "old_start_min": old_min,
        "old_start_hm": old_hm,
        "new_start_min": new_min,
        "new_start_hm": window.start_hm,
        "reason": reason,
    }
    return window, shift


def detect_overrun_shift(
    *,
    running: Occupied,
    later: PlannedWindow,
    occupied: list[Occupied],
    now: datetime,
    quiet_start: str = DEFAULT_QUIET_START,
    quiet_end: str = DEFAULT_QUIET_END,
    scan_hour: int = DEFAULT_SCAN_HOUR,
    drill_hour: int = DEFAULT_DRILL_HOUR,
) -> tuple[PlannedWindow, dict[str, Any]] | None:
    """If a running job overlaps a later window on the same host, shift the later one."""
    if later.status in (STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED, STATUS_RUNNING):
        return None
    if running.target_id != later.target_id:
        return None
    if not kinds_conflict(running.kind, later.kind) and not (
        running.kind == later.kind == KIND_PATCH
    ):
        return None
    run_end = running.start_min + running.duration_min
    later_end = later.start_min + later.duration_min
    if not ranges_overlap(
        running.start_min, running.duration_min, later.start_min, later.duration_min
    ):
        return None
    if run_end <= later.start_min:
        return None
    reason = (
        REASON_BACKUP_OVERRUN
        if running.kind in (KIND_BACKUP, KIND_RESTORE)
        else REASON_PATCH_OVERRUN
    )
    if later.start_min >= later_end:
        return None
    return shift_later_window(
        later,
        occupied,
        now=now,
        running_end_min=run_end,
        reason=reason,
        scan_hour=scan_hour,
        drill_hour=drill_hour,
        quiet_start=quiet_start,
        quiet_end=quiet_end,
    )
