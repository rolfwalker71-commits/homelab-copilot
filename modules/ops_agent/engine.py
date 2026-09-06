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
    REASON_HOST_GONE,
    REASON_OUT_OF_FOCUS,
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
from ops_agent.hosts import collect_live_hosts, split_inventory_changes
from ops_agent.actor import actor_fields, agent_phrase, by_agent
from ops_agent.image_snaps import ImageSnap, remember_after_image, snap_from_job_result
from ops_agent.policy import ConfirmPolicy, in_job_scope
from ops_agent.rollback import (
    job_kind_label_de,
    plan_rollback,
    reason_label_de,
    window_reason_after_rollback,
)
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
        delete_guest_snap: Callable[[str, str], Awaitable[Any]] | None = None,
        rollback_guest_snap: Callable[[str, str], Awaitable[Any]] | None = None,
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
        self._delete_guest_snap = delete_guest_snap
        self._rollback_guest_snap = rollback_guest_snap
        self._lock = asyncio.Lock()

    @staticmethod
    def _is_image_window(row: dict[str, Any]) -> bool:
        return str(row.get("bucket") or "") == "images" or str(row.get("kind") or "") == "image"

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

    async def gone_ids(self) -> set[str]:
        return {
            str(h.get("target_id") or "")
            for h in await self.store.list_known_hosts()
            if h.get("gone") and str(h.get("target_id") or "")
        }

    async def _live_hosts(self) -> list[dict[str, Any]]:
        snap = self._get_snapshot()
        manuals: list[dict[str, Any]] = []
        try:
            from patcher.module import _get_store as _pstore
            from patcher.targets import manual_targets

            pstore = _pstore()
            if pstore is not None:
                manuals = [t.to_dict() for t in await manual_targets(pstore)]
        except Exception:
            manuals = []
        return collect_live_hosts(snap, manuals)

    async def reconcile_hosts(self) -> list[dict[str, Any]]:
        """Ask about new/removed guests. First empty known-set is seeded, no flood."""
        live = await self._live_hosts()
        known = await self.store.list_known_hosts()
        if not known:
            if live:
                await self.store.seed_known_hosts(live)
            return []
        pending = await self.store.list_scope_prompts(status="waiting")
        pending_ids = {str(p.get("target_id") or "") for p in pending if p.get("target_id")}
        live_by_id = {str(h["id"]): h for h in live}
        live_ids = set(live_by_id)
        present = {
            str(h.get("target_id") or "")
            for h in known
            if not h.get("gone") and h.get("target_id")
        }
        gone = {
            str(h.get("target_id") or "")
            for h in known
            if h.get("gone") and h.get("target_id")
        }
        known_by_id = {str(h.get("target_id") or ""): h for h in known}
        appeared, disappeared, returned = split_inventory_changes(
            live_ids=live_ids,
            known_present_ids=present,
            known_gone_ids=gone,
            pending_ids=pending_ids,
        )
        created: list[dict[str, Any]] = []
        for tid in sorted(appeared):
            host = live_by_id[tid]
            await self.store.upsert_known_host(
                target_id=tid,
                target_name=str(host.get("name") or tid),
                kind=str(host.get("kind") or ""),
                gone=False,
            )
            pid = await self.store.insert_scope_prompt(
                target_id=tid,
                target_name=str(host.get("name") or tid),
                kind="appeared",
                reason=(
                    f"Neuer Host/Gast {host.get('name') or tid} — "
                    "automatisch zu Patching? zu Image-Update?"
                ),
            )
            if pid:
                row = await self.store.get_scope_prompt(pid)
                if row:
                    created.append(row)
        for tid in sorted(disappeared):
            host = known_by_id.get(tid) or {}
            name = str(host.get("target_name") or tid)
            await self.store.mark_known_gone(tid)
            pid = await self.store.insert_scope_prompt(
                target_id=tid,
                target_name=name,
                kind="disappeared",
                reason=f"{name} ist weg — aus Patch- und Image-Liste nehmen?",
            )
            if pid:
                row = await self.store.get_scope_prompt(pid)
                if row:
                    created.append(row)
        for tid in sorted(returned):
            host = live_by_id[tid]
            waiting = await self.store.find_waiting_prompt(tid)
            if waiting and waiting.get("kind") == "disappeared":
                await self.store.dismiss_scope_prompt(
                    int(waiting["id"]),
                    reason="Host ist wieder da — Frage zurückgezogen.",
                )
            await self.store.mark_known_present(
                tid,
                target_name=str(host.get("name") or tid),
                kind=str(host.get("kind") or ""),
            )
        for tid in live_ids & present:
            host = live_by_id[tid]
            waiting = await self.store.find_waiting_prompt(tid)
            if waiting and waiting.get("kind") == "disappeared":
                await self.store.dismiss_scope_prompt(
                    int(waiting["id"]),
                    reason="Host ist wieder da — Frage zurückgezogen.",
                )
            await self.store.mark_known_present(
                tid,
                target_name=str(host.get("name") or tid),
                kind=str(host.get("kind") or ""),
            )
        leftover = await self.store.list_scope_prompts(status="waiting")
        for prompt in leftover:
            tid = str(prompt.get("target_id") or "")
            if prompt.get("kind") == "appeared" and tid and tid not in live_ids:
                await self.store.dismiss_scope_prompt(
                    int(prompt["id"]),
                    reason="Wieder weg, bevor du entschieden hast.",
                )
                await self.store.mark_known_gone(tid)
        return created

    async def _release_ok_image_snap(self) -> None:
        rec = await self.store.get_ok_image_snap()
        if not rec:
            return
        if self._delete_guest_snap is not None:
            try:
                await self._delete_guest_snap(rec["target_id"], rec["snap_name"])
            except Exception:
                logger.info(
                    "Erfolgreichen Image-Snapshot nicht gelöscht",
                    extra={"target_id": rec.get("target_id")},
                    exc_info=True,
                )
        await self.store.clear_ok_image_snap()

    async def _remember_image_snap(self, row: dict[str, Any], job: Any) -> None:
        created = snap_from_job_result(
            str(row.get("target_id") or ""),
            getattr(job, "result", None) if job is not None else None,
        )
        last = await self.store.get_ok_image_snap()
        last_snap = (
            ImageSnap(last["target_id"], last["snap_name"]) if last else None
        )
        nxt = remember_after_image(
            last_snap,
            ok=str(getattr(job, "status", "") or "") == "success",
            created=created,
        )
        if nxt is None:
            await self.store.clear_ok_image_snap()
        else:
            await self.store.set_ok_image_snap(nxt.target_id, nxt.name)
        remaining = await self.store.list_windows(
            statuses=[STATUS_ACCEPTED, STATUS_WAITING]
        )
        more_images = [
            w
            for w in remaining
            if self._is_image_window(w) and w.get("kind") == KIND_PATCH
        ]
        if not more_images:
            await self._release_ok_image_snap()

    async def save_scope(
        self, *, patch_scope_ids: list[str], image_scope_ids: list[str]
    ) -> ConfirmPolicy:
        policy = await self.store.get_policy()
        policy.patch_scope_ids = [str(x).strip() for x in patch_scope_ids if str(x).strip()]
        policy.image_scope_ids = [str(x).strip() for x in image_scope_ids if str(x).strip()]
        saved = await self.store.save_policy(policy)
        for tid in set(saved.patch_scope_ids) | set(saved.image_scope_ids):
            waiting = await self.store.find_waiting_prompt(tid)
            if waiting and waiting.get("kind") == "appeared":
                await self.store.answer_scope_prompt(
                    int(waiting["id"]),
                    patch=tid in saved.patch_scope_ids,
                    image=tid in saved.image_scope_ids,
                )
        return saved

    async def answer_host_prompt(
        self,
        prompt_id: int,
        *,
        patch: bool | None = None,
        image: bool | None = None,
        drop: bool | None = None,
    ) -> dict[str, Any]:
        row = await self.store.get_scope_prompt(prompt_id)
        if not row or row.get("status") != "waiting":
            raise RuntimeError("Keine offene Host-Frage.")
        policy = await self.store.get_policy()
        tid = str(row.get("target_id") or "")
        kind = str(row.get("kind") or "")
        if kind == "appeared":
            want_patch = bool(patch)
            want_image = bool(image)
            patch_ids = [x for x in policy.patch_scope_ids if x.lower() != tid.lower()]
            image_ids = [x for x in policy.image_scope_ids if x.lower() != tid.lower()]
            if want_patch:
                patch_ids.append(tid)
            if want_image:
                image_ids.append(tid)
            policy.patch_scope_ids = patch_ids
            policy.image_scope_ids = image_ids
            await self.store.save_policy(policy)
            await self.store.answer_scope_prompt(
                prompt_id, patch=want_patch, image=want_image
            )
        elif kind == "disappeared":
            if drop:
                policy.patch_scope_ids = [
                    x for x in policy.patch_scope_ids if x.lower() != tid.lower()
                ]
                policy.image_scope_ids = [
                    x for x in policy.image_scope_ids if x.lower() != tid.lower()
                ]
                await self.store.save_policy(policy)
                await self.store.delete_known_host(tid)
            else:
                await self.store.upsert_known_host(
                    target_id=tid,
                    target_name=str(row.get("target_name") or tid),
                    kind="",
                    gone=True,
                    keep_preference=True,
                )
            await self.store.answer_scope_prompt(prompt_id, drop_from_scope=bool(drop))
        else:
            raise RuntimeError("Unbekannte Host-Frage.")
        fresh = await self.store.get_scope_prompt(prompt_id)
        assert fresh is not None
        return fresh

    async def scope_matrix(self) -> dict[str, Any]:
        policy = await self.policy()
        live = await self._live_hosts()
        known = await self.store.list_known_hosts()
        live_ids = {str(h["id"]) for h in live}
        by_id: dict[str, dict[str, Any]] = {str(h["id"]): dict(h) for h in live}
        for k in known:
            tid = str(k.get("target_id") or "")
            if not tid:
                continue
            if tid not in by_id:
                by_id[tid] = {
                    "id": tid,
                    "target_id": tid,
                    "name": k.get("target_name") or tid,
                    "kind": k.get("kind") or "",
                    "kind_label": "Host",
                    "node": "",
                    "online": False,
                    "present": False,
                }
            row = by_id[tid]
            row["present"] = tid in live_ids
            row["gone"] = bool(k.get("gone")) and tid not in live_ids
            row["keep_preference"] = bool(k.get("keep_preference"))
        patch = {x.lower() for x in policy.patch_scope_ids}
        image = {x.lower() for x in policy.image_scope_ids}
        hosts = sorted(by_id.values(), key=lambda r: str(r.get("name") or "").lower())
        for h in hosts:
            tid = str(h.get("id") or "").lower()
            h["patch"] = tid in patch
            h["image"] = tid in image
            if h.get("gone"):
                h["present"] = False
        prompts = await self.store.list_scope_prompts(status="waiting")
        return {
            "hosts": hosts,
            "patch_scope_ids": list(policy.patch_scope_ids),
            "image_scope_ids": list(policy.image_scope_ids),
            "prompts": prompts,
        }

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
                gone = await self.gone_ids()
                for host in hosts:
                    known.add(host.target_id)
                    for item in group_host_work(host):
                        if not in_job_scope(
                            policy,
                            kind=KIND_PATCH,
                            bucket=item.bucket,
                            target_id=item.target_id,
                            gone_ids=gone,
                        ):
                            continue
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
        await self.reconcile_hosts()
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
            gone_ids=await self.gone_ids(),
        )
        created: list[dict[str, Any]] = []
        for win in planned:
            row = self._attach_times(win, now)
            if not auto_apply and win.status == STATUS_ACCEPTED:
                row["status"] = STATUS_WAITING
                row["needs_confirm"] = True
                row["reason"] = "Vorschlag — Agent ist aus, daher noch nicht übernommen."
            if str(row.get("source") or "") == SOURCE_AGENT:
                reason = str(row.get("reason") or "").strip()
                row["reason"] = (
                    by_agent(reason) if reason else agent_phrase("window_planned")
                )
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
            if str(row.get("source") or "") == SOURCE_AGENT:
                reason = str(row.get("reason") or "").strip()
                row["reason"] = by_agent(reason) if reason else agent_phrase("window_planned")
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
                shifted_reason = by_agent(moved.reason or agent_phrase("window_shifted", via_agent=False))
                await self.store.update_window(
                    int(row["id"]),
                    start_iso=new_iso,
                    start_hm=moved.start_hm,
                    reason=shifted_reason,
                )
                await self.store.add_shift(
                    int(row["id"]),
                    old_start_iso=old_iso,
                    old_start_hm=rec["old_start_hm"],
                    new_start_iso=new_iso,
                    new_start_hm=rec["new_start_hm"],
                    reason=by_agent(rec["reason"]),
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
                            agent_phrase("window_shifted"),
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
                if not in_job_scope(
                    await self.policy(),
                    kind=kind,
                    bucket=str(row.get("bucket") or ""),
                    target_id=tid,
                    gone_ids=await self.gone_ids(),
                ):
                    await self.store.update_window(
                        int(row["id"]),
                        status=STATUS_SKIPPED,
                        reason=REASON_OUT_OF_FOCUS,
                    )
                    continue
                if any(j for j in self._list_patch_jobs() if str(getattr(j, "kind", "")) in ("apply", "image-apply")):
                    continue
                if self._is_image_window(row):
                    await self._release_ok_image_snap()
                ok, err, job_id = await self._start_patch(row)
                if ok:
                    await self.store.update_window(
                        int(row["id"]), status=STATUS_RUNNING, job_id=job_id
                    )
                    started.append({**row, "status": STATUS_RUNNING, "job_id": job_id})
                else:
                    job = self._lookup_patch_job(job_id or "")
                    await self._fail_patch_window(
                        row,
                        job=job,
                        job_id=job_id,
                        error=err or "Patch fehlgeschlagen.",
                    )
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

    def _lookup_patch_job(self, job_id: str) -> Any:
        jid = str(job_id or "").strip()
        if not jid:
            return None
        try:
            from patcher.jobs import JOBS

            return JOBS.get(jid)
        except Exception:
            return None

    def _log_job_line(self, job_id: str, line: str) -> None:
        jid = str(job_id or "").strip()
        if not jid or not line:
            return
        try:
            from patcher.jobs import JOBS

            JOBS.append_log(jid, line)
        except Exception:
            logger.info("Rollback-Log am Job nicht geschrieben", exc_info=True)

    def _attach_job_rollback(self, job: Any, rec: dict[str, Any]) -> None:
        if job is None:
            return
        prev = dict(getattr(job, "result", None) or {})
        prev["rollback"] = {
            "status": rec.get("status"),
            "snap_name": rec.get("snap_name") or "",
            "reason": rec.get("reason") or "",
            "error": rec.get("error") or "",
            **actor_fields(via_agent=True),
        }
        try:
            job.result = prev
        except Exception:
            logger.info("Rollback am Job-Result nicht gesetzt", exc_info=True)

    async def _maybe_rollback_failed_apply(
        self,
        row: dict[str, Any],
        job: Any,
        *,
        error: str,
    ) -> dict[str, Any]:
        """One autonomous rollback per job. Never raises. DistUpgrade is never eligible."""
        job_id = str(
            (getattr(job, "id", None) if job is not None else None)
            or row.get("job_id")
            or ""
        )
        existing = await self.store.get_rollback_for_job(job_id) if job_id else None
        if existing is None and row.get("id") is not None:
            existing = await self.store.get_rollback_for_window(int(row["id"]))
        if existing:
            self._attach_job_rollback(job, existing)
            return existing

        kind = str(
            (getattr(job, "kind", None) if job is not None else None)
            or ("image-apply" if self._is_image_window(row) else "apply")
        )
        target_id = str(
            row.get("target_id")
            or (getattr(job, "target_id", "") if job is not None else "")
            or ""
        )
        result = getattr(job, "result", None) if job is not None else None
        logs = list(getattr(job, "log_lines", None) or []) if job is not None else None
        plan = plan_rollback(
            job_kind=kind,
            target_id=target_id,
            result=result if isinstance(result, dict) else None,
            error=error,
            log_lines=logs,
        )
        target_name = str(row.get("target_name") or target_id)
        window_id = int(row["id"]) if row.get("id") is not None else None

        if plan.action != "rollback":
            rec = await self.store.insert_rollback(
                window_id=window_id,
                job_id=job_id,
                target_id=target_id,
                target_name=target_name,
                job_kind=plan.job_kind,
                snap_name=plan.snap_name,
                reason=plan.reason_code,
                status="skipped",
                error=plan.skip_reason,
            )
            self._log_job_line(job_id, by_agent(plan.skip_reason))
            self._attach_job_rollback(job, rec)
            logger.info(
                "Autonomes Rollback übersprungen: %s",
                plan.skip_reason,
                extra={"target_id": target_id, "job_kind": kind},
            )
            return rec

        rb_ok = False
        rb_err = ""
        if self._rollback_guest_snap is None:
            rb_err = "Keine Rollback-Funktion."
        else:
            try:
                raw = await self._rollback_guest_snap(target_id, plan.snap_name)
                if isinstance(raw, dict) and raw.get("ok") is False:
                    rb_err = str(raw.get("error") or "Rollback fehlgeschlagen.")
                else:
                    rb_ok = True
            except Exception as exc:
                rb_err = getattr(exc, "message", None) or str(exc)

        rec = await self.store.insert_rollback(
            window_id=window_id,
            job_id=job_id,
            target_id=target_id,
            target_name=target_name,
            job_kind=plan.job_kind,
            snap_name=plan.snap_name,
            reason=plan.reason_code,
            status="ok" if rb_ok else "failed",
            error="" if rb_ok else rb_err,
        )
        if rb_ok:
            self._log_job_line(
                job_id,
                agent_phrase("rolled_back", snap=plan.snap_name)
                + f" ({reason_label_de(plan.reason_code)}). Snapshot bleibt "
                "bis PATCHER_SNAP_KEEP.",
            )
        else:
            self._log_job_line(
                job_id,
                by_agent(
                    f"Rollback auf „{plan.snap_name}“ fehlgeschlagen: {rb_err} "
                    "Kein weiterer Versuch."
                ),
            )
        self._attach_job_rollback(job, rec)
        if self._notify_shift is not None:
            try:
                title = (
                    agent_phrase("rolled_back", snap=plan.snap_name)
                    if rb_ok
                    else agent_phrase("rollback_failed", snap=plan.snap_name)
                )
                body = (
                    f"{target_name}: Snapshot {plan.snap_name} "
                    f"({reason_label_de(plan.reason_code)})."
                    if rb_ok
                    else f"{target_name}: {rb_err}"
                )
                await self._notify_shift(title, body)
            except Exception:
                logger.info("Push nach Rollback fehlgeschlagen", exc_info=True)
        return rec

    async def _fail_patch_window(
        self,
        row: dict[str, Any],
        *,
        job: Any,
        job_id: str | None,
        error: str,
        halt_wave: bool = True,
    ) -> str:
        rec = await self._maybe_rollback_failed_apply(
            {**row, "job_id": job_id or row.get("job_id") or ""},
            job,
            error=error,
        )
        reason = window_reason_after_rollback(error, rec)
        await self.store.update_window(
            int(row["id"]),
            status=STATUS_FAILED,
            job_id=job_id or row.get("job_id"),
            reason=reason,
        )
        if self._is_image_window(row) and job is not None:
            await self._remember_image_snap(row, job)
        if halt_wave:
            await self.store.save_settings(patch_halted=True)
            await self._skip_remaining_patches(agent_phrase("wave_stopped"))
        return reason

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
                    if self._is_image_window(row):
                        await self._remember_image_snap(row, job)
                elif st == "failed":
                    await self._fail_patch_window(
                        row,
                        job=job,
                        job_id=job_id,
                        error=str(getattr(job, "error", "") or "Patch fehlgeschlagen."),
                    )

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
            if not in_job_scope(
                await self.policy(),
                kind=kind,
                bucket=str(row.get("bucket") or ""),
                target_id=tid,
                gone_ids=await self.gone_ids(),
            ):
                await self.store.update_window(
                    window_id, status=STATUS_SKIPPED, reason=REASON_OUT_OF_FOCUS
                )
                raise RuntimeError(REASON_OUT_OF_FOCUS)
            if self._is_image_window(row):
                await self._release_ok_image_snap()
            ok, err, job_id = await self._start_patch(row)
            if not ok:
                job = self._lookup_patch_job(job_id or "")
                reason = await self._fail_patch_window(
                    row,
                    job=job,
                    job_id=job_id,
                    error=err or "Patch nicht gestartet.",
                )
                raise RuntimeError(reason)
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
                await self.reconcile_hosts()
            except Exception:
                logger.exception("Agent-Host-Abgleich fehlgeschlagen")
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
        try:
            await self.reconcile_hosts()
        except Exception:
            logger.exception("Host-Abgleich für Board")
        policy = await self.policy()
        settings = await self.settings()
        matrix = await self.scope_matrix()
        all_rows = await self.store.list_windows(limit=300)
        shifts = await self.store.list_shifts(limit=30)
        rollbacks = await self.store.list_rollbacks(limit=40)
        rb_by_window = {
            int(r["window_id"]): r
            for r in rollbacks
            if r.get("window_id") is not None
        }
        live_backup = [j.to_dict() for j in self._list_backup_jobs()]
        live_patch = [j.to_dict() for j in self._list_patch_jobs()]
        next_up: list[dict[str, Any]] = []
        waiting: list[dict[str, Any]] = []
        running: list[dict[str, Any]] = []
        done: list[dict[str, Any]] = []
        for row in all_rows:
            wid = row.get("id")
            packed = self._serialize_window(
                row,
                now,
                rollback=rb_by_window.get(int(wid)) if wid is not None else None,
            )
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
        rollback_events = [self._serialize_rollback(r) for r in rollbacks]
        shifted: list[dict[str, Any]] = [
            {
                "event": "shift",
                "id": s.get("id"),
                "window_id": s.get("window_id"),
                "kind": s.get("kind"),
                "target_name": s.get("target_name"),
                "stack": s.get("stack"),
                "old_start_hm": s.get("old_start_hm"),
                "new_start_hm": s.get("new_start_hm"),
                "reason": s.get("reason"),
                "created_at": s.get("created_at"),
                "created_at_iso": s.get("created_at_iso"),
                **actor_fields(via_agent=True),
            }
            for s in shifts
        ]
        for ev in rollback_events:
            if ev.get("status") != "failed":
                shifted.append(ev)
        shifted.sort(
            key=lambda r: str(r.get("created_at_iso") or r.get("created_at") or ""),
            reverse=True,
        )
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
            "hosts": matrix["hosts"],
            "scope": {
                "patch_ids": matrix["patch_scope_ids"],
                "image_ids": matrix["image_scope_ids"],
            },
            "scope_prompts": matrix["prompts"],
            "next": next_up,
            "running": running,
            "waiting": waiting,
            "rollback_failed": [e for e in rollback_events if e.get("status") == "failed"],
            "shifted": shifted,
            "rollbacks": rollback_events,
            "done": done[:40],
            "live_jobs": {"backup": live_backup, "patch": live_patch},
            "time": format_de(now),
            "timezone": "Europe/Berlin",
        }

    def _serialize_rollback(self, rec: dict[str, Any]) -> dict[str, Any]:
        status = str(rec.get("status") or "")
        status_label = {
            "ok": "Zurückgesetzt",
            "failed": "Rollback fehlgeschlagen",
            "skipped": "Rollback übersprungen",
        }.get(status, status)
        out = {
            "event": "rollback",
            "id": rec.get("id"),
            "window_id": rec.get("window_id"),
            "kind": "rollback",
            "job_kind": rec.get("job_kind"),
            "job_kind_label": job_kind_label_de(str(rec.get("job_kind") or "")),
            "target_id": rec.get("target_id"),
            "target_name": rec.get("target_name"),
            "snap_name": rec.get("snap_name") or "",
            "reason": rec.get("reason"),
            "reason_label": reason_label_de(str(rec.get("reason") or "")),
            "status": status,
            "status_label": status_label,
            "error": rec.get("error") or "",
            "created_at": rec.get("created_at"),
            "created_at_iso": rec.get("created_at_iso"),
            **actor_fields(via_agent=True),
        }
        return out

    def _serialize_window(
        self,
        row: dict[str, Any],
        now: datetime,
        *,
        rollback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
        via = str(row.get("source") or "") == SOURCE_AGENT
        out.update(actor_fields(via_agent=via))
        if rollback:
            out["rollback"] = self._serialize_rollback(rollback)
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
