"""Ops-Agent runtime: ingest, propose, watch/shift, start existing engines."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from app.core.locale import BERLIN, format_de, iso_utc, now_berlin
from app.core.snapshots import guest_can_snapshot
from backup_verifier.cron import preset_to_cron
from backup_verifier.planner import classify_stacks, parse_hhmm
from backup_verifier.scheduler import next_run_after, schedule_clock_hm
from ops_agent.config import get_ops_settings
from ops_agent.planner import (
    DURATION_BACKUP,
    KIND_BACKUP,
    KIND_DRILL,
    KIND_PATCH,
    KIND_RESTORE,
    Occupied,
    PlannedWindow,
    REASON_BACKUP_OVERRUN,
    REASON_PATCH_OVERRUN,
    SOURCE_AGENT,
    SOURCE_DRILL,
    SOURCE_INGESTED,
    STATUS_ACCEPTED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_WAITING,
    abs_minutes,
    day_start,
    detect_overrun_shift,
    dt_from_abs,
    duration_for,
    ingest_schedule_windows,
    occupied_from_windows,
    propose_windows,
    Need,
)
from ops_agent.policy import ConfirmPolicy, default_policy
from ops_agent.store import OpsStore
from patcher.agent import (
    HostPending,
    disk_critical_threshold,
    evaluate_gates,
    group_host_work,
    host_context_from_snapshot,
)

logger = logging.getLogger(__name__)

StartBackupFn = Callable[[dict[str, Any]], Awaitable[str | None]]
StartPatchFn = Callable[[dict[str, Any]], Awaitable[tuple[bool, str, str | None]]]


@dataclass
class EffectiveSettings:
    enabled: bool
    shift_auto: bool
    quiet_start: str
    quiet_end: str
    patch_halted: bool


class OpsEngine:
    def __init__(
        self,
        store: OpsStore,
        *,
        get_snapshot: Callable[[], Any],
        get_backup_store: Callable[[], Any],
        list_backup_stacks: Callable[[Any], Awaitable[list[dict[str, Any]]]],
        hosts_from_store: Callable[..., Awaitable[list[HostPending]]],
        start_backup: StartBackupFn,
        start_patch: StartPatchFn,
        list_backup_jobs: Callable[[], list[Any]],
        list_patch_jobs: Callable[[], list[Any]],
        get_inventory_tags: Callable[[str], Awaitable[list[str]]] | None = None,
        notify_shift: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self._get_snapshot = get_snapshot
        self._get_backup_store = get_backup_store
        self._list_backup_stacks = list_backup_stacks
        self._hosts_from_store = hosts_from_store
        self._start_backup = start_backup
        self._start_patch = start_patch
        self._list_backup_jobs = list_backup_jobs
        self._list_patch_jobs = list_patch_jobs
        self._get_inventory_tags = get_inventory_tags
        self._notify_shift = notify_shift
        self._lock = asyncio.Lock()

    async def settings(self) -> EffectiveSettings:
        env = get_ops_settings()
        row = await self.store.get_settings()
        enabled = env.ops_agent_enabled if row.get("enabled") is None else bool(row["enabled"])
        shift = (
            env.ops_agent_shift_auto
            if row.get("shift_auto") is None
            else bool(row["shift_auto"])
        )
        return EffectiveSettings(
            enabled=enabled,
            shift_auto=shift,
            quiet_start=str(row.get("quiet_start") or env.ops_agent_quiet_start),
            quiet_end=str(row.get("quiet_end") or env.ops_agent_quiet_end),
            patch_halted=bool(row.get("patch_halted")),
        )

    async def policy(self) -> ConfirmPolicy:
        return await self.store.get_policy()

    def _attach_times(self, win: PlannedWindow, now: datetime) -> dict[str, Any]:
        dt = dt_from_abs(now, win.start_min)
        row = win.to_row()
        row["start_iso"] = iso_utc(dt)
        row["start_hm"] = win.start_hm
        return row

    async def ingest(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Mirror saved backup schedules (+ drill) onto the board. Never deletes them."""
        now = now or now_berlin()
        bstore = self._get_backup_store()
        if bstore is None:
            return []
        schedules = await bstore.list_schedules()
        snap = self._get_snapshot()
        enriched: list[dict[str, Any]] = []
        for row in schedules:
            item = dict(row)
            nxt = (
                next_run_after(str(row.get("cron_expr") or ""), now)
                if row.get("enabled")
                else None
            )
            if nxt is not None:
                item["next_run_iso"] = iso_utc(nxt)
                item["start_hm"] = nxt.astimezone(BERLIN).strftime("%H:%M")
            else:
                hm = schedule_clock_hm(row)
                if hm:
                    item["start_hm"] = f"{hm[0]:02d}:{hm[1]:02d}"
            if snap is not None:
                from backup_verifier.inventory import resolve_guest

                info = resolve_guest(snap, str(row.get("parent_id") or ""))
                item["guest_name"] = info.get("guest_name") or item.get("guest_name") or ""
            enriched.append(item)
        from backup_verifier.config import get_backup_settings

        bs = get_backup_settings()
        planned = ingest_schedule_windows(
            enriched,
            now=now,
            drill_enabled=bool(bs.backup_drill_enabled),
            drill_hour=int(bs.backup_drill_hour),
        )
        upserted: list[dict[str, Any]] = []
        for win in planned:
            row = self._attach_times(win, now)
            if win.schedule_id:
                existing = await self.store.find_by_schedule(int(win.schedule_id))
                if existing:
                    if existing.get("status") in (
                        STATUS_RUNNING,
                        STATUS_DONE,
                        STATUS_FAILED,
                    ):
                        upserted.append(existing)
                        continue
                    await self.store.update_window(
                        int(existing["id"]),
                        start_iso=row["start_iso"],
                        start_hm=row["start_hm"],
                        target_name=row["target_name"],
                        reason=row["reason"],
                    )
                    upserted.append(await self.store.get_window(int(existing["id"])) or existing)
                    continue
            if win.source == SOURCE_DRILL:
                open_drill = await self.store.find_open_for_target(
                    kind=KIND_DRILL, target_id="*", stack=""
                )
                if open_drill:
                    await self.store.update_window(
                        int(open_drill["id"]),
                        start_iso=row["start_iso"],
                        start_hm=row["start_hm"],
                    )
                    upserted.append(await self.store.get_window(int(open_drill["id"])) or open_drill)
                    continue
            wid = await self.store.insert_window(row)
            upserted.append(await self.store.get_window(wid) or row)
        return upserted

    async def _collect_needs(self, policy: ConfirmPolicy) -> list[Need]:
        snap = self._get_snapshot()
        needs: list[Need] = []
        known: set[str] = set()
        scheduled_parents: set[str] = set()
        bstore = self._get_backup_store()
        schedules: list[dict[str, Any]] = []
        if bstore is not None:
            schedules = await bstore.list_schedules()
            for row in schedules:
                pid = str(row.get("parent_id") or "")
                if pid:
                    known.add(pid)
                    scheduled_parents.add(pid)
        try:
            from patcher.agent import hosts_from_store as _hosts

            patcher_store = None
            try:
                from patcher.module import _get_store as _pstore

                patcher_store = _pstore()
            except Exception:
                patcher_store = None
            if patcher_store is not None and snap is not None:
                hosts = await _hosts(
                    patcher_store, snap, tags_for=self._get_inventory_tags
                )
                for host in hosts:
                    known.add(host.target_id)
                    for item in group_host_work(host):
                        needs.append(
                            Need(
                                kind=KIND_PATCH,
                                target_id=item.target_id,
                                target_name=item.target_name,
                                bucket=item.bucket,
                                duration_min=duration_for(
                                    kind=KIND_PATCH, bucket=item.bucket
                                ),
                                packages=list(item.packages),
                                confirm_reasons=list(item.confirm_reasons),
                                tags=list(host.tags),
                                known_host=True,
                                source=SOURCE_AGENT,
                            )
                        )
        except Exception:
            logger.exception("Pending patches für Agent nicht lesbar")

        if snap is not None and bstore is not None:
            try:
                discovered = await self._list_backup_stacks(snap)
                split = classify_stacks(discovered, schedules)
                for row in split["missing"]:
                    parent_id = str(row.get("parent_id") or "")
                    stack = str(row.get("stack") or "")
                    if not parent_id or not stack:
                        continue
                    tags: list[str] = []
                    if self._get_inventory_tags is not None:
                        try:
                            tags = list(await self._get_inventory_tags(parent_id))
                        except Exception:
                            tags = []
                    needs.append(
                        Need(
                            kind=KIND_BACKUP,
                            target_id=parent_id,
                            target_name=str(row.get("guest_name") or parent_id),
                            stack=stack,
                            bucket=KIND_BACKUP,
                            duration_min=DURATION_BACKUP,
                            tags=tags,
                            known_host=parent_id in known,
                            has_existing_schedule=False,
                            engine=str(row.get("engine") or "tar"),
                            source=SOURCE_AGENT,
                        )
                    )
            except Exception:
                logger.exception("Backup-Lücken für Agent nicht lesbar")
        return needs

    async def _host_maps(self) -> tuple[dict[str, bool], dict[str, bool], set[str], set[str]]:
        snap = self._get_snapshot()
        online: dict[str, bool] = {}
        disk: dict[str, bool] = {}
        running: set[str] = set()
        no_snap: set[str] = set()
        threshold = disk_critical_threshold()
        ids: set[str] = set()
        if snap is not None:
            for attr in ("guests", "hosts", "nodes"):
                for ent in list(getattr(snap, attr, None) or []):
                    eid = getattr(ent, "id", None)
                    if eid:
                        ids.add(str(eid))
        for tid in ids:
            ctx, _tags = host_context_from_snapshot(snap, tid)
            online[tid] = ctx.online
            if ctx.disk_pct is not None:
                try:
                    disk[tid] = float(ctx.disk_pct) >= float(threshold)
                except (TypeError, ValueError):
                    disk[tid] = False
            gates = evaluate_gates(ctx, disk_critical_pct=threshold)
            if any("nicht online" in g for g in gates):
                online[tid] = False
        for job in self._list_backup_jobs():
            kind = str(getattr(job, "kind", "") or "")
            if kind not in (KIND_BACKUP, KIND_RESTORE, ""):
                continue
            pid = str(getattr(job, "parent_id", "") or "")
            if pid:
                running.add(pid)
        for job in self._list_patch_jobs():
            kind = str(getattr(job, "kind", "") or "")
            if kind not in ("apply", "image-apply", "apply-batch"):
                continue
            tid = str(getattr(job, "target_id", "") or "")
            if tid:
                running.add(tid)
        for tid in ids:
            if str(tid).startswith(("lxc:", "qemu:")) and not guest_can_snapshot(tid):
                no_snap.add(tid)
        return online, disk, running, no_snap

    async def propose(self, *, auto_apply: bool = True) -> dict[str, Any]:
        """Compute next windows. After policy, accepted rows run themselves."""
        now = now_berlin()
        await self.ingest(now=now)
        policy = await self.policy()
        settings = await self.settings()
        needs = await self._collect_needs(policy)
        existing = await self.store.list_windows(
            statuses=[STATUS_ACCEPTED, STATUS_WAITING, STATUS_RUNNING]
        )
        occupied = self._occupied_from_rows(existing, now)
        online, disk, running, no_snap = await self._host_maps()
        open_keys = {
            (str(w.get("kind")), str(w.get("target_id")), str(w.get("stack") or ""))
            for w in existing
            if w.get("source") == SOURCE_AGENT
        }
        fresh: list[Need] = []
        for need in needs:
            key = (need.kind, need.target_id, need.stack)
            if key in open_keys:
                continue
            fresh.append(need)
        planned, skipped = propose_windows(
            fresh,
            occupied,
            now=now,
            policy=policy,
            quiet_start=settings.quiet_start,
            quiet_end=settings.quiet_end,
            host_online=online,
            disk_critical=disk,
            running_targets=running,
            snap_unavailable=no_snap,
        )
        created: list[dict[str, Any]] = []
        for win in planned:
            row = self._attach_times(win, now)
            if not auto_apply and win.status == STATUS_ACCEPTED:
                row["status"] = STATUS_WAITING
                row["needs_confirm"] = True
                row["reason"] = "Vorschlag — Agent ist aus, daher noch nicht übernommen."
            wid = await self.store.insert_window(row)
            created.append(await self.store.get_window(wid) or row)
            if (
                settings.enabled
                and win.kind == KIND_BACKUP
                and win.status == STATUS_ACCEPTED
                and auto_apply
            ):
                await self._ensure_backup_schedule(created[-1], now)
        for win in skipped:
            await self.store.delete_skipped_for(
                kind=win.kind, target_id=win.target_id, stack=win.stack
            )
            row = win.to_row()
            row["start_iso"] = iso_utc(now)
            row["start_hm"] = now.strftime("%H:%M")
            await self.store.insert_window(row)
        return {
            "ok": True,
            "created": created,
            "skipped": [s.to_row() for s in skipped],
            "policy": policy.to_dict(),
            "enabled": settings.enabled,
            "auto_applied": bool(auto_apply and settings.enabled),
            "message": (
                f"{len(created)} Fenster geplant"
                + (" und übernommen." if auto_apply else ".")
                + (f" {len(skipped)} übersprungen." if skipped else "")
            ),
        }

    def _occupied_from_rows(
        self, rows: list[dict[str, Any]], now: datetime
    ) -> list[Occupied]:
        origin = day_start(now)
        out: list[Occupied] = []
        for row in rows:
            start = self._row_abs(row, origin)
            out.append(
                Occupied(
                    target_id=str(row.get("target_id") or ""),
                    kind=str(row.get("kind") or ""),
                    start_min=start,
                    duration_min=int(row.get("duration_min") or 10),
                    global_backup=str(row.get("kind") or "")
                    in (KIND_BACKUP, KIND_DRILL),
                    label=str(row.get("stack") or row.get("target_name") or ""),
                )
            )
        return out

    def _row_abs(self, row: dict[str, Any], origin: datetime) -> int:
        iso = str(row.get("start_iso") or "")
        if iso:
            try:
                dt = datetime.fromisoformat(iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return abs_minutes(dt.astimezone(BERLIN), origin)
            except ValueError:
                pass
        hm = str(row.get("start_hm") or "20:00")
        try:
            return parse_hhmm(hm)
        except Exception:
            return 20 * 60

    def _row_as_planned(self, row: dict[str, Any], now: datetime) -> PlannedWindow:
        origin = day_start(now)
        return PlannedWindow(
            kind=str(row.get("kind") or ""),
            target_id=str(row.get("target_id") or ""),
            target_name=str(row.get("target_name") or ""),
            stack=str(row.get("stack") or ""),
            bucket=str(row.get("bucket") or ""),
            start_min=self._row_abs(row, origin),
            start_hm=str(row.get("start_hm") or ""),
            duration_min=int(row.get("duration_min") or 10),
            status=str(row.get("status") or STATUS_ACCEPTED),
            source=str(row.get("source") or SOURCE_AGENT),
            schedule_id=row.get("schedule_id"),
            needs_confirm=bool(row.get("needs_confirm")),
            confirm_reasons=list(row.get("confirm_reasons") or []),
            gates=list(row.get("gates") or []),
            packages=list(row.get("packages") or []),
            reason=str(row.get("reason") or ""),
            engine=str(row.get("engine") or "tar"),
        )

    async def _ensure_backup_schedule(
        self, window: dict[str, Any], now: datetime
    ) -> None:
        bstore = self._get_backup_store()
        if bstore is None:
            return
        if window.get("schedule_id"):
            return
        parent_id = str(window.get("target_id") or "")
        stack = str(window.get("stack") or "")
        if not parent_id or not stack:
            return
        existing = await bstore.find_schedules_for_stack(parent_id, stack)
        hm = str(window.get("start_hm") or "20:00")
        try:
            expr = preset_to_cron("daily", hm)
        except Exception:
            logger.exception("Cron für Agent-Backup nicht gebaut")
            return
        if existing:
            sid = int(existing[0]["id"])
            await bstore.upsert_schedule(
                schedule_id=sid,
                stack=stack,
                parent_id=parent_id,
                cron_expr=expr,
                preset="daily",
                enabled=True,
                note="ops-agent",
                engine=str(window.get("engine") or existing[0].get("engine") or "tar"),
                restic_full_every_days=int(existing[0].get("restic_full_every_days") or 7),
                restic_keep_last=int(existing[0].get("restic_keep_last") or 14),
                restic_keep_weekly=int(existing[0].get("restic_keep_weekly") or 8),
            )
        else:
            sid = await bstore.upsert_schedule(
                schedule_id=None,
                stack=stack,
                parent_id=parent_id,
                cron_expr=expr,
                preset="daily",
                enabled=True,
                note="ops-agent",
                engine=str(window.get("engine") or "tar"),
            )
        await self.store.update_window(int(window["id"]), schedule_id=sid)

    async def watch_and_shift(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        settings = await self.settings()
        if not settings.enabled or not settings.shift_auto:
            return []
        now = now or now_berlin()
        origin = day_start(now)
        windows = await self.store.list_windows(
            statuses=[STATUS_ACCEPTED, STATUS_WAITING, STATUS_RUNNING]
        )
        occupied = self._occupied_from_rows(windows, now)
        running_jobs: list[Occupied] = []
        for job in self._list_backup_jobs():
            kind = str(getattr(job, "kind", "") or KIND_BACKUP)
            if kind not in (KIND_BACKUP, KIND_RESTORE):
                continue
            created = float(getattr(job, "created_at", 0) or 0)
            start = datetime.fromtimestamp(created, tz=BERLIN) if created else now
            dur = max(20, int((now - start).total_seconds() // 60) + 10)
            running_jobs.append(
                Occupied(
                    target_id=str(getattr(job, "parent_id", "") or ""),
                    kind=kind,
                    start_min=abs_minutes(start, origin),
                    duration_min=dur,
                    global_backup=kind == KIND_BACKUP,
                )
            )
        for job in self._list_patch_jobs():
            kind = str(getattr(job, "kind", "") or "")
            if kind not in ("apply", "image-apply"):
                continue
            created = float(getattr(job, "created_at", 0) or 0)
            start = datetime.fromtimestamp(created, tz=BERLIN) if created else now
            dur = max(20, int((now - start).total_seconds() // 60) + 10)
            running_jobs.append(
                Occupied(
                    target_id=str(getattr(job, "target_id", "") or ""),
                    kind=KIND_PATCH,
                    start_min=abs_minutes(start, origin),
                    duration_min=dur,
                )
            )
        shifts: list[dict[str, Any]] = []
        for run in running_jobs:
            for row in list(windows):
                later = self._row_as_planned(row, now)
                occ = occupied + running_jobs
                result = detect_overrun_shift(
                    running=run,
                    later=later,
                    occupied=occ,
                    now=now,
                    quiet_start=settings.quiet_start,
                    quiet_end=settings.quiet_end,
                )
                if result is None:
                    continue
                moved, rec = result
                old_iso = str(row.get("start_iso") or "")
                new_dt = dt_from_abs(now, moved.start_min)
                new_iso = iso_utc(new_dt)
                await self.store.update_window(
                    int(row["id"]),
                    start_iso=new_iso,
                    start_hm=moved.start_hm,
                    reason=moved.reason,
                )
                await self.store.add_shift(
                    int(row["id"]),
                    old_start_iso=old_iso,
                    old_start_hm=rec["old_start_hm"],
                    new_start_iso=new_iso,
                    new_start_hm=rec["new_start_hm"],
                    reason=rec["reason"],
                )
                if row.get("schedule_id") and moved.kind == KIND_BACKUP:
                    await self._rewrite_schedule_time(
                        int(row["schedule_id"]), moved.start_hm
                    )
                row["start_iso"] = new_iso
                row["start_hm"] = moved.start_hm
                rec["window_id"] = int(row["id"])
                rec["target_name"] = row.get("target_name")
                rec["kind"] = row.get("kind")
                shifts.append(rec)
                if self._notify_shift is not None:
                    try:
                        await self._notify_shift(
                            "Fenster verschoben",
                            f"{row.get('target_name') or row.get('stack')}: "
                            f"{rec['old_start_hm']} → {rec['new_start_hm']}. {rec['reason']}",
                        )
                    except Exception:
                        logger.info("Push nach Verschieben fehlgeschlagen", exc_info=True)
        return shifts

    async def _rewrite_schedule_time(self, schedule_id: int, start_hm: str) -> None:
        bstore = self._get_backup_store()
        if bstore is None:
            return
        row = await bstore.get_schedule(schedule_id)
        if not row:
            return
        try:
            expr = preset_to_cron(str(row.get("preset") or "daily"), start_hm)
        except Exception:
            expr = preset_to_cron("daily", start_hm)
        await bstore.upsert_schedule(
            schedule_id=schedule_id,
            stack=str(row.get("stack") or ""),
            parent_id=str(row.get("parent_id") or ""),
            cron_expr=expr,
            preset=str(row.get("preset") or "daily"),
            enabled=bool(row.get("enabled", True)),
            note=str(row.get("note") or "ops-agent"),
            engine=str(row.get("engine") or "tar"),
            restic_full_every_days=int(row.get("restic_full_every_days") or 7),
            restic_keep_last=int(row.get("restic_keep_last") or 14),
            restic_keep_weekly=int(row.get("restic_keep_weekly") or 8),
        )

    async def start_due(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        settings = await self.settings()
        if not settings.enabled:
            return []
        now = now or now_berlin()
        started: list[dict[str, Any]] = []
        windows = await self.store.list_windows(statuses=[STATUS_ACCEPTED])
        online, disk, running, no_snap = await self._host_maps()
        for row in windows:
            if str(row.get("source") or "") == SOURCE_DRILL:
                continue
            start = self._parse_start(row)
            if start is None or start > now + timedelta(seconds=30):
                continue
            tid = str(row.get("target_id") or "")
            kind = str(row.get("kind") or "")
            if kind == KIND_BACKUP and row.get("schedule_id"):
                # Existing scheduler fires these — only mark running when a job appears.
                continue
            if tid in running:
                continue
            if not online.get(tid, True):
                continue
            if disk.get(tid):
                continue
            if kind == KIND_PATCH:
                if settings.patch_halted:
                    continue
                if any(j for j in self._list_patch_jobs() if str(getattr(j, "kind", "")) in ("apply", "image-apply")):
                    continue
                ok, err, job_id = await self._start_patch(row)
                if ok:
                    await self.store.update_window(
                        int(row["id"]), status=STATUS_RUNNING, job_id=job_id
                    )
                    started.append({**row, "status": STATUS_RUNNING, "job_id": job_id})
                else:
                    await self.store.update_window(
                        int(row["id"]),
                        status=STATUS_FAILED,
                        job_id=job_id,
                        reason=err or "Patch fehlgeschlagen.",
                    )
                    await self.store.save_settings(patch_halted=True)
                    await self._skip_remaining_patches(err or "Welle gestoppt nach Fehler.")
            elif kind == KIND_BACKUP:
                job_id = await self._start_backup(row)
                await self.store.update_window(
                    int(row["id"]), status=STATUS_RUNNING, job_id=job_id
                )
                started.append({**row, "status": STATUS_RUNNING, "job_id": job_id})
        return started

    async def _skip_remaining_patches(self, reason: str) -> None:
        rows = await self.store.list_windows(statuses=[STATUS_ACCEPTED, STATUS_WAITING])
        for row in rows:
            if row.get("kind") != KIND_PATCH:
                continue
            await self.store.update_window(
                int(row["id"]), status=STATUS_SKIPPED, reason=reason
            )

    def _parse_start(self, row: dict[str, Any]) -> datetime | None:
        iso = str(row.get("start_iso") or "")
        if not iso:
            return None
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(BERLIN)
        except ValueError:
            return None

    async def sync_jobs(self) -> None:
        windows = await self.store.list_windows(statuses=[STATUS_RUNNING, STATUS_ACCEPTED])
        backup_jobs = {str(getattr(j, "id", "")): j for j in self._list_backup_jobs()}
        # Also look at finished jobs still in registry
        try:
            from backup_verifier.jobs import JOBS as BACKUP_JOBS

            for job in list(getattr(BACKUP_JOBS, "_jobs", {}).values()):
                backup_jobs[str(job.id)] = job
        except Exception:
            pass
        try:
            from patcher.jobs import JOBS as PATCH_JOBS

            patch_all = list(getattr(PATCH_JOBS, "_jobs", {}).values())
        except Exception:
            patch_all = []

        for row in windows:
            kind = str(row.get("kind") or "")
            job_id = str(row.get("job_id") or "")
            if kind == KIND_BACKUP and not job_id:
                parent = str(row.get("target_id") or "")
                stack = str(row.get("stack") or "")
                for job in backup_jobs.values():
                    if (
                        str(getattr(job, "parent_id", "")) == parent
                        and str(getattr(job, "project", "")) == stack
                        and str(getattr(job, "kind", "backup")) in ("backup", "")
                    ):
                        job_id = str(job.id)
                        await self.store.update_window(
                            int(row["id"]), job_id=job_id, status=STATUS_RUNNING
                        )
                        break
            if not job_id:
                continue
            if kind == KIND_BACKUP:
                job = backup_jobs.get(job_id)
                if job is None:
                    continue
                st = str(getattr(job, "status", "") or "")
                if st in ("success", "partial"):
                    await self.store.update_window(int(row["id"]), status=STATUS_DONE)
                elif st == "failed":
                    await self.store.update_window(
                        int(row["id"]),
                        status=STATUS_FAILED,
                        reason=str(getattr(job, "error", "") or "Backup fehlgeschlagen."),
                    )
            elif kind == KIND_PATCH:
                job = next((j for j in patch_all if str(j.id) == job_id), None)
                if job is None:
                    continue
                st = str(getattr(job, "status", "") or "")
                if st == "success":
                    await self.store.update_window(int(row["id"]), status=STATUS_DONE)
                    await self.store.save_settings(patch_halted=False)
                elif st == "failed":
                    await self.store.update_window(
                        int(row["id"]),
                        status=STATUS_FAILED,
                        reason=str(getattr(job, "error", "") or "Patch fehlgeschlagen."),
                    )
                    await self.store.save_settings(patch_halted=True)
                    await self._skip_remaining_patches("Welle gestoppt nach Apply-Fehler.")

    async def start_now(self, window_id: int) -> dict[str, Any]:
        row = await self.store.get_window(window_id)
        if not row:
            raise RuntimeError("Fenster nicht gefunden.")
        if row.get("kind") == KIND_DRILL:
            raise RuntimeError("Restore-Drill startet der bestehende Drill-Scheduler.")
        online, disk, running, _no_snap = await self._host_maps()
        tid = str(row.get("target_id") or "")
        if not online.get(tid, True):
            raise RuntimeError("Host ist nicht online.")
        if disk.get(tid):
            raise RuntimeError("Disk ist kritisch — Start blockiert.")
        if tid in running:
            raise RuntimeError("Auf diesem Ziel läuft bereits ein Auftrag.")
        kind = str(row.get("kind") or "")
        if kind == KIND_PATCH:
            ok, err, job_id = await self._start_patch(row)
            if not ok:
                await self.store.update_window(
                    window_id, status=STATUS_FAILED, job_id=job_id, reason=err
                )
                raise RuntimeError(err or "Patch nicht gestartet.")
            await self.store.update_window(window_id, status=STATUS_RUNNING, job_id=job_id)
        elif kind == KIND_BACKUP:
            job_id = await self._start_backup(row)
            await self.store.update_window(window_id, status=STATUS_RUNNING, job_id=job_id)
        else:
            raise RuntimeError("Dieser Fenstertyp lässt sich hier nicht starten.")
        result = await self.store.get_window(window_id)
        assert result is not None
        return result

    async def confirm_window(self, window_id: int) -> dict[str, Any]:
        row = await self.store.get_window(window_id)
        if not row:
            raise RuntimeError("Fenster nicht gefunden.")
        await self.store.update_window(
            window_id,
            status=STATUS_ACCEPTED,
            needs_confirm=False,
            reason="Von dir bestätigt — Agent übernimmt den Start.",
        )
        if row.get("kind") == KIND_BACKUP:
            fresh = await self.store.get_window(window_id)
            if fresh:
                await self._ensure_backup_schedule(fresh, now_berlin())
        start = self._parse_start(row)
        if start is not None and start <= now_berlin() + timedelta(minutes=1):
            return await self.start_now(window_id)
        result = await self.store.get_window(window_id)
        assert result is not None
        return result

    async def decline_window(self, window_id: int) -> dict[str, Any]:
        row = await self.store.get_window(window_id)
        if not row:
            raise RuntimeError("Fenster nicht gefunden.")
        await self.store.update_window(
            window_id,
            status=STATUS_SKIPPED,
            reason="Abgelehnt.",
        )
        result = await self.store.get_window(window_id)
        assert result is not None
        return result

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self.ingest()
            except Exception:
                logger.exception("Agent-Ingest fehlgeschlagen")
            try:
                await self.sync_jobs()
            except Exception:
                logger.exception("Agent-Job-Sync fehlgeschlagen")
            try:
                await self.watch_and_shift()
            except Exception:
                logger.exception("Agent-Verschieben fehlgeschlagen")
            try:
                await self.start_due()
            except Exception:
                logger.exception("Agent-Start fehlgeschlagen")

    async def board(self) -> dict[str, Any]:
        now = now_berlin()
        try:
            await self.ingest(now=now)
        except Exception:
            logger.exception("Ingest für Board")
        try:
            await self.sync_jobs()
        except Exception:
            logger.exception("Sync für Board")
        policy = await self.policy()
        settings = await self.settings()
        all_rows = await self.store.list_windows(limit=300)
        shifts = await self.store.list_shifts(limit=30)
        live_backup = [j.to_dict() for j in self._list_backup_jobs()]
        live_patch = [j.to_dict() for j in self._list_patch_jobs()]
        next_up: list[dict[str, Any]] = []
        waiting: list[dict[str, Any]] = []
        running: list[dict[str, Any]] = []
        done: list[dict[str, Any]] = []
        for row in all_rows:
            packed = self._serialize_window(row, now)
            st = str(row.get("status") or "")
            if st == STATUS_WAITING:
                waiting.append(packed)
            elif st == STATUS_RUNNING:
                running.append(packed)
            elif st in (STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED):
                done.append(packed)
            elif st == STATUS_ACCEPTED:
                next_up.append(packed)
        done.sort(key=lambda r: str(r.get("updated_at_iso") or r.get("start_iso") or ""), reverse=True)
        return {
            "ok": True,
            "enabled": settings.enabled,
            "shift_auto": settings.shift_auto,
            "quiet_start": settings.quiet_start,
            "quiet_end": settings.quiet_end,
            "quiet_rule": (
                "Neue Fenster: 20:00–23:50 Europe/Berlin, nach der letzten Backup-Welle, "
                "nicht während 04:00-Scan oder 05:00-Drill. Bestehende Zeitpläne bleiben."
            ),
            "policy": policy.to_dict(),
            "next": next_up,
            "running": running,
            "waiting": waiting,
            "shifted": [
                {
                    "id": s.get("id"),
                    "window_id": s.get("window_id"),
                    "kind": s.get("kind"),
                    "target_name": s.get("target_name"),
                    "stack": s.get("stack"),
                    "old_start_hm": s.get("old_start_hm"),
                    "new_start_hm": s.get("new_start_hm"),
                    "reason": s.get("reason"),
                    "created_at": s.get("created_at"),
                }
                for s in shifts
            ],
            "done": done[:40],
            "live_jobs": {"backup": live_backup, "patch": live_patch},
            "time": format_de(now),
            "timezone": "Europe/Berlin",
        }

    def _serialize_window(self, row: dict[str, Any], now: datetime) -> dict[str, Any]:
        start = self._parse_start(row)
        out = dict(row)
        out["start_de"] = format_de(start, with_uhr=True) if start else row.get("start_hm")
        out["kind_label"] = {
            KIND_BACKUP: "Backup",
            KIND_PATCH: "Patch",
            KIND_DRILL: "Restore-Drill",
        }.get(str(row.get("kind") or ""), row.get("kind"))
        bucket = str(row.get("bucket") or "")
        out["bucket_label"] = {
            "security": "Security",
            "regular": "Bestätigung",
            "images": "Images",
            "backup": "Backup",
            "drill": "Drill",
        }.get(bucket, bucket)
        out["can_start"] = str(row.get("status") or "") in (
            STATUS_ACCEPTED,
            STATUS_WAITING,
        ) and str(row.get("kind") or "") != KIND_DRILL
        dur = int(row.get("duration_min") or 10)
        out["duration_label"] = f"{dur} min"
        return out


async def run_ops_loop(engine: OpsEngine, *, poll_seconds: float = 15.0) -> None:
    logger.info("Ops-Agent-Schleife aktiv (Europe/Berlin)")
    await asyncio.sleep(8)
    while True:
        try:
            await engine.tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ops-Agent-Tick fehlgeschlagen")
        await asyncio.sleep(max(5.0, float(poll_seconds)))
