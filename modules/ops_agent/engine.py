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
from backup_verifier.planner import format_hhmm, parse_hhmm
from backup_verifier.scheduler import minute_key, next_run_after, schedule_clock_hm
from ops_agent.config import get_ops_settings
from ops_agent.planner import (
    DURATION_BACKUP,
    KIND_BACKUP,
    KIND_DRILL,
    KIND_PATCH,
    KIND_RESTORE,
    Occupied,
    PlannedWindow,
    REASON_BACKUP_CHAIN,
    REASON_BACKUP_OVERRUN,
    REASON_DEST_FULL,
    REASON_HOST_GONE,
    REASON_HOST_OFFLINE_CHAIN,
    REASON_HUNG,
    REASON_OUT_OF_FOCUS,
    REASON_CAPACITY_WARN,
    REASON_COPILOT_DATA,
    REASON_EOL_PROPOSE,
    REASON_OFFLINE_TODAY,
    REASON_PATCH_OVERRUN,
    REASON_PRUNE,
    REASON_REBOOT_DONE,
    REASON_REBOOT_NO_API,
    REASON_REBOOT_WAIT,
    REASON_SMART_WARN,
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
    clock_of,
    day_start,
    detect_overrun_shift,
    dt_from_abs,
    duration_for,
    next_free_slot,
    snap_10_minutes,
    ingest_schedule_windows,
    occupied_from_windows,
    propose_windows,
    Need,
)
from ops_agent.hosts import (
    COPILOT_DATA_ID,
    belongs_in_host_matrix,
    collect_live_hosts,
    exclude_synthetic_ids,
    is_live_backup_target,
    is_synthetic_copilot_data,
    split_inventory_changes,
)
from ops_agent.activity import (
    ACTION_APPLY,
    ACTION_BACKUP_CHAIN,
    ACTION_BRIEF,
    ACTION_PLANNED,
    ACTION_PRUNE,
    ACTION_REBOOT,
    ACTION_ROLLBACK,
    ACTION_SHIFTED,
    ACTION_SKIPPED,
    ACTION_STARTED,
    ACTION_WARN,
    RESULT_FAIL,
    RESULT_INFO,
    RESULT_OK,
    RESULT_SKIP,
    RESULT_WAIT,
    build_evening_brief,
    serialize_activity,
)
from ops_agent.actor import actor_fields, agent_phrase, by_agent
from ops_agent.capacity import (
    collect_dests,
    dest_is_critically_full,
    estimate_bytes_from_runs,
    job_fits,
    warn_lines,
)
from ops_agent.image_snaps import ImageSnap, remember_after_image, snap_from_job_result
from ops_agent.lessons import (
    classify_error_class,
    error_short,
    host_kind_of,
    job_kind_of,
    next_action_de,
    package_names,
    packages_key,
    scan_apply_note,
    serialize_lesson,
    should_hold,
    why_de,
)
from ops_agent.policy import ConfirmPolicy, in_job_scope, is_hard_stop, needs_human
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
RebootFn = Callable[[str], Awaitable[dict[str, Any] | None]]
PruneFn = Callable[[str], Awaitable[dict[str, Any] | None]]
DestUsageFn = Callable[[], Awaitable[dict[str, Any]]]
SmartFn = Callable[[], Awaitable[list[dict[str, Any]]]]

COPILOT_STACK_HINTS = ("homelab-copilot", "hlops-data", "copilot-data")


def _schedule_method_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Engine + retention from a saved Backup-page schedule. Timing is not included.

    restic stays restic. Keep/full-every values are passed through as stored
    (0 is valid for keep_weekly). Defaults apply only when a field is missing.
    """
    raw = str(row.get("engine") or "").strip().lower()
    engine = "restic" if raw == "restic" else "tar"

    def _retain_int(key: str, default: int) -> int:
        if key not in row or row[key] is None or row[key] == "":
            return default
        return int(row[key])

    return {
        "engine": engine,
        "restic_full_every_days": _retain_int("restic_full_every_days", 7),
        "restic_keep_last": _retain_int("restic_keep_last", 14),
        "restic_keep_weekly": _retain_int("restic_keep_weekly", 8),
    }


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
        reboot_host: RebootFn | None = None,
        prune_images: PruneFn | None = None,
        dest_usage: DestUsageFn | None = None,
        smart_signals: SmartFn | None = None,
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
        self._reboot_host = reboot_host
        self._prune_images = prune_images
        self._dest_usage = dest_usage
        self._smart_signals = smart_signals
        self._lock = asyncio.Lock()
        self._last_proactive = 0.0

    @staticmethod
    def _is_image_window(row: dict[str, Any]) -> bool:
        return str(row.get("bucket") or "") == "images" or str(row.get("kind") or "") == "image"

    async def _log_activity(
        self,
        action: str,
        *,
        result: str = RESULT_INFO,
        kind: str = "",
        target_id: str = "",
        target_name: str = "",
        window_id: int | None = None,
        detail: str = "",
        row: dict[str, Any] | None = None,
    ) -> None:
        if row is not None:
            target_id = target_id or str(row.get("target_id") or "")
            target_name = target_name or str(row.get("target_name") or target_id)
            kind = kind or str(row.get("kind") or "")
            if window_id is None and row.get("id") is not None:
                try:
                    window_id = int(row["id"])
                except (TypeError, ValueError):
                    window_id = None
        try:
            await self.store.insert_activity(
                action=action,
                result=result,
                kind=kind,
                target_id=target_id,
                target_name=target_name,
                window_id=window_id,
                detail=by_agent(detail) if detail else "",
            )
        except Exception:
            logger.info("Tätigkeitslog nicht geschrieben", exc_info=True)

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
        return exclude_synthetic_ids(
            str(h.get("target_id") or "")
            for h in await self.store.list_known_hosts()
            if h.get("gone") and str(h.get("target_id") or "")
        )

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
        await self._forget_synthetic_copilot_host()
        live = await self._live_hosts()
        known = await self.store.list_known_hosts()
        if not known:
            if live:
                await self.store.seed_known_hosts(live)
                await self._dismiss_vanished_no_backup_prompts(
                    {str(h["id"]) for h in live}
                )
                await self._prompt_missing_backups(live)
                await self._prompt_copilot_data()
            return []
        pending = await self.store.list_scope_prompts(status="waiting")
        pending_ids = exclude_synthetic_ids(
            str(p.get("target_id") or "") for p in pending if p.get("target_id")
        )
        live_by_id = {str(h["id"]): h for h in live}
        live_ids = set(live_by_id)
        present = exclude_synthetic_ids(
            str(h.get("target_id") or "")
            for h in known
            if not h.get("gone") and h.get("target_id")
        )
        gone = exclude_synthetic_ids(
            str(h.get("target_id") or "")
            for h in known
            if h.get("gone") and h.get("target_id")
        )
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
                    "automatisch zu Patching? zu Image-Update? Backup einplanen?"
                ),
            )
            if pid:
                row = await self.store.get_scope_prompt(pid)
                if row:
                    created.append(row)
                    await self._notify_waiting(
                        "Wartet auf dich",
                        f"Neuer Host/Gast {host.get('name') or tid}.",
                    )
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
            if is_synthetic_copilot_data(tid):
                continue
            if prompt.get("kind") == "appeared" and tid and tid not in live_ids:
                await self.store.dismiss_scope_prompt(
                    int(prompt["id"]),
                    reason="Wieder weg, bevor du entschieden hast.",
                )
                await self.store.mark_known_gone(tid)
        await self._dismiss_vanished_no_backup_prompts(live_ids)
        await self._prompt_missing_backups(live)
        await self._prompt_copilot_data()
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
        backup: bool | None = None,
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
            await self.store.set_skip_backup(tid, skip=not bool(backup))
            if backup:
                planned = await self._plan_backup_for_host(
                    tid, str(row.get("target_name") or tid)
                )
                if not planned:
                    await self._log_unplanned_backup(
                        tid, str(row.get("target_name") or tid)
                    )
            await self.store.answer_scope_prompt(
                prompt_id, patch=want_patch, image=want_image, backup=bool(backup)
            )
        elif kind == "no_backup":
            await self.store.set_skip_backup(tid, skip=not bool(backup))
            if backup:
                if tid == COPILOT_DATA_ID:
                    planned = await self._plan_copilot_data_if_possible()
                    if not planned:
                        await self._log_unplanned_backup(tid, "Copilot /data")
                else:
                    planned = await self._plan_backup_for_host(
                        tid, str(row.get("target_name") or tid)
                    )
                    if not planned:
                        await self._log_unplanned_backup(
                            tid, str(row.get("target_name") or tid)
                        )
            await self.store.answer_scope_prompt(prompt_id, backup=bool(backup))
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
            if is_synthetic_copilot_data(tid):
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
        hosts = sorted(
            (row for row in by_id.values() if belongs_in_host_matrix(row)),
            key=lambda r: str(r.get("name") or "").lower(),
        )
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
        gone = await self.gone_ids()
        enriched: list[dict[str, Any]] = []
        for row in schedules:
            parent = str(row.get("parent_id") or "").strip()
            if parent and parent in gone:
                continue
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
                try:
                    from backup_verifier.inventory import resolve_guest

                    info = resolve_guest(snap, str(row.get("parent_id") or ""))
                    item["guest_name"] = (
                        info.get("guest_name") or item.get("guest_name") or ""
                    )
                except Exception:
                    pass
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
                    existing_reason = str(existing.get("reason") or "")
                    if "Anschluss" in existing_reason or "durch Agent" in existing_reason:
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
        """Patch needs only. Backup windows come from ingest of saved schedules."""
        snap = self._get_snapshot()
        needs: list[Need] = []
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

        # Backups come only from saved Backup-page schedules (ingest), never
        # from compose discovery. Missing stacks stay a prompt, not a Need.
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
            await self._log_activity(
                ACTION_PLANNED,
                result=RESULT_OK if str(row.get("status") or "") == STATUS_ACCEPTED else RESULT_WAIT,
                row=created[-1],
                detail=str(row.get("reason") or "Fenster geplant"),
            )
            if str(row.get("status") or "") == STATUS_WAITING:
                await self._notify_waiting(
                    "Wartet auf dich",
                    f"{row.get('target_name') or row.get('target_id')}: {row.get('reason') or 'Bestätigung'}",
                )
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
            await self._log_activity(
                ACTION_SKIPPED,
                result=RESULT_SKIP,
                row=row,
                detail=str(row.get("reason") or "Übersprungen"),
            )
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
        """Attach an existing Backup-page schedule. Never invent a new one."""
        if window.get("schedule_id"):
            return
        bstore = self._get_backup_store()
        if bstore is None:
            return
        parent_id = str(window.get("target_id") or "")
        stack = str(window.get("stack") or "")
        if not parent_id or not stack:
            return
        existing = await bstore.find_schedules_for_stack(parent_id, stack)
        if not existing:
            return
        restic = [
            r
            for r in existing
            if str(r.get("engine") or "").strip().lower() == "restic"
        ]
        chosen = restic[0] if restic else existing[0]
        await self.store.update_window(
            int(window["id"]), schedule_id=int(chosen["id"])
        )

    async def watch_and_shift(
        self, *, now: datetime | None = None, force: bool = False
    ) -> list[dict[str, Any]]:
        settings = await self.settings()
        if not force and (not settings.enabled or not settings.shift_auto):
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
                await self._log_activity(
                    ACTION_SHIFTED,
                    result=RESULT_OK,
                    row=row,
                    detail=f"{rec['old_start_hm']} → {rec['new_start_hm']}: {rec['reason']}",
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
                rec["reason"] = by_agent(rec.get("reason") or "")
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
        method = _schedule_method_fields(row)
        await bstore.upsert_schedule(
            schedule_id=schedule_id,
            stack=str(row.get("stack") or ""),
            parent_id=str(row.get("parent_id") or ""),
            cron_expr=expr,
            preset=str(row.get("preset") or "daily"),
            enabled=bool(row.get("enabled", True)),
            note=str(row.get("note") or ""),
            **method,
        )

    async def start_due(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        settings = await self.settings()
        if not settings.enabled:
            return []
        now = now or now_berlin()
        started: list[dict[str, Any]] = []
        await self._warn_capacity()
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
            if tid in running:
                continue
            if kind == KIND_BACKUP:
                if await self._any_backup_running():
                    continue
                skip = await self._backup_skip_reason(
                    row, online=online, disk=disk, running=running
                )
                if skip:
                    await self._apply_backup_skip(row, skip, now=now)
                    continue
                job_id = await self._start_backup(row)
                await self.store.update_window(
                    int(row["id"]), status=STATUS_RUNNING, job_id=job_id
                )
                await self._mark_schedule_used(row)
                await self._log_activity(
                    ACTION_STARTED,
                    result=RESULT_OK,
                    row=row,
                    detail="Backup gestartet",
                )
                started.append({**row, "status": STATUS_RUNNING, "job_id": job_id})
                running.add(tid)
                await self._pack_backup_chain(
                    now=now, start_min=self._chain_cursor(now)
                )
                continue
            if not online.get(tid, True):
                await self._flag_offline_visible(row, kind_label="Patch")
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
                held = await self._hold_for_lesson(row)
                if held:
                    continue
                if self._is_image_window(row):
                    await self._release_ok_image_snap()
                ok, err, job_id = await self._start_patch(row)
                if ok:
                    await self.store.update_window(
                        int(row["id"]), status=STATUS_RUNNING, job_id=job_id
                    )
                    await self._note_scan_on_job(row, job_id)
                    await self._log_activity(
                        ACTION_STARTED,
                        result=RESULT_OK,
                        row=row,
                        kind="image" if self._is_image_window(row) else KIND_PATCH,
                        detail="Apply gestartet",
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
        await self._log_activity(
            ACTION_ROLLBACK,
            result=RESULT_OK if rb_ok else RESULT_FAIL,
            kind=str(row.get("kind") or KIND_PATCH),
            target_id=target_id,
            target_name=target_name,
            window_id=window_id,
            detail=(
                f"Rollback auf {plan.snap_name}"
                if rb_ok
                else f"Rollback fehlgeschlagen: {rb_err}"
            ),
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
        await self._record_lesson(row, job=job, job_id=job_id, error=error, rollback=rec)
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
        finished_any = False
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
                    await self._log_activity(
                        ACTION_APPLY,
                        result=RESULT_OK,
                        kind=KIND_BACKUP,
                        row=row,
                        detail="Backup erfolgreich",
                    )
                    finished_any = True
                elif st == "failed":
                    await self.store.update_window(
                        int(row["id"]),
                        status=STATUS_FAILED,
                        reason=str(getattr(job, "error", "") or "Backup fehlgeschlagen."),
                    )
                    await self._log_activity(
                        ACTION_APPLY,
                        result=RESULT_FAIL,
                        kind=KIND_BACKUP,
                        row=row,
                        detail=str(getattr(job, "error", "") or "Backup fehlgeschlagen."),
                    )
                    finished_any = True
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
                    await self._log_activity(
                        ACTION_APPLY,
                        result=RESULT_OK,
                        kind="image" if self._is_image_window(row) else KIND_PATCH,
                        row=row,
                        detail="Apply erfolgreich",
                    )
                    await self._maybe_reboot_after_apply(row, job)
                    if self._is_image_window(row):
                        await self._maybe_prune_after_image(row)
                    finished_any = True
                elif st == "failed":
                    await self._fail_patch_window(
                        row,
                        job=job,
                        job_id=job_id,
                        error=str(getattr(job, "error", "") or "Patch fehlgeschlagen."),
                    )
                    await self._log_activity(
                        ACTION_APPLY,
                        result=RESULT_FAIL,
                        kind="image" if self._is_image_window(row) else KIND_PATCH,
                        row=row,
                        detail=str(getattr(job, "error", "") or "Patch fehlgeschlagen."),
                    )
                    finished_any = True
        if finished_any:
            await self._advance_backup_chain()
            await self._maybe_refresh_brief()
        await self._flag_hung_jobs()

    def _window_job_kind(self, row: dict[str, Any]) -> str:
        return job_kind_of(kind=str(row.get("kind") or ""), bucket=str(row.get("bucket") or ""))

    def _window_packages(self, row: dict[str, Any]) -> list[str]:
        return package_names(row.get("packages"))

    async def _hold_for_lesson(self, row: dict[str, Any]) -> str | None:
        names = self._window_packages(row)
        hold = should_hold(
            await self.store.list_lessons(limit=80),
            packages_key=packages_key(names, bucket=str(row.get("bucket") or "")),
            host_kind=host_kind_of(str(row.get("target_id") or "")),
            job_kind=self._window_job_kind(row),
        )
        if hold is None:
            return None
        await self.store.update_window(
            int(row["id"]),
            status=STATUS_WAITING,
            needs_confirm=True,
            reason=hold.reason,
        )
        return hold.reason

    async def _record_lesson(
        self,
        row: dict[str, Any],
        *,
        job: Any,
        job_id: str | None,
        error: str,
        rollback: dict[str, Any] | None,
    ) -> dict[str, Any]:
        names = self._window_packages(row)
        bucket = str(row.get("bucket") or "")
        host_kind = host_kind_of(str(row.get("target_id") or ""))
        job_kind = self._window_job_kind(row)
        err_class = classify_error_class(error)
        rollback_ran = str((rollback or {}).get("status") or "") == "ok"
        why = why_de(err_class)
        nxt = next_action_de(host_kind=host_kind, job_kind=job_kind)
        rec = await self.store.insert_lesson(
            window_id=int(row["id"]) if row.get("id") is not None else None,
            job_id=str(job_id or row.get("job_id") or ""),
            target_id=str(row.get("target_id") or ""),
            target_name=str(row.get("target_name") or ""),
            host_kind=host_kind,
            job_kind=job_kind,
            packages=names,
            packages_key=packages_key(names, bucket=bucket),
            error_class=err_class,
            error_short=error_short(error),
            why_de=why,
            next_de=nxt,
            rollback_ran=rollback_ran,
        )
        line = by_agent(f"Lektion: {why.rstrip('.')} {nxt}")
        self._log_job_line(str(job_id or row.get("job_id") or ""), line)
        self._attach_job_lesson(job, rec)
        return rec

    def _attach_job_lesson(self, job: Any, rec: dict[str, Any]) -> None:
        if job is None:
            return
        prev = dict(getattr(job, "result", None) or {})
        prev["lesson"] = serialize_lesson(rec)
        try:
            job.result = prev
        except Exception:
            logger.info("Lektion am Job-Result nicht gesetzt", exc_info=True)

    async def _note_scan_on_job(self, row: dict[str, Any], job_id: str | None) -> None:
        """Surface reboot/breaks only if the latest scan already has them. No USN fetch."""
        note = await self._scan_note_for_window(row)
        if not note:
            return
        jid = str(job_id or row.get("job_id") or "")
        self._log_job_line(jid, by_agent(note))
        job = self._lookup_patch_job(jid)
        if job is None:
            return
        prev = dict(getattr(job, "result", None) or {})
        prev["scan_note"] = note
        try:
            job.result = prev
        except Exception:
            logger.info("Scan-Hinweis am Job nicht gesetzt", exc_info=True)

    async def _scan_note_for_window(self, row: dict[str, Any]) -> str | None:
        tid = str(row.get("target_id") or "")
        if not tid:
            return None
        try:
            from patcher.module import _get_store as _pstore

            pstore = _pstore()
            if pstore is None:
                return None
            scan = await pstore.latest_scan_for_target(tid)
            if not scan:
                return None
            pkgs: list[dict[str, Any]] = []
            sid = scan.get("id")
            if sid is not None and hasattr(pstore, "list_packages"):
                try:
                    pkgs = list(await pstore.list_packages(int(sid)))
                except Exception:
                    pkgs = []
            return scan_apply_note(
                job_packages=self._window_packages(row),
                reboot_required=bool(scan.get("reboot_required")),
                scan_packages=pkgs,
            )
        except Exception:
            return None

    def _guest_kinds(self) -> frozenset[str]:
        return frozenset({"lxc", "qemu", "manual"})

    async def _covered_backup_ids(self) -> set[str]:
        covered: set[str] = set()
        bstore = self._get_backup_store()
        if bstore is not None:
            try:
                for row in await bstore.list_schedules():
                    pid = str(row.get("parent_id") or "")
                    if pid:
                        covered.add(pid)
            except Exception:
                pass
        for w in await self.store.list_windows(
            statuses=[STATUS_ACCEPTED, STATUS_WAITING, STATUS_RUNNING]
        ):
            if str(w.get("kind") or "") == KIND_BACKUP:
                tid = str(w.get("target_id") or "")
                if tid:
                    covered.add(tid)
        return covered

    async def _prompt_missing_backups(self, live: list[dict[str, Any]]) -> None:
        live_ids = {
            str(h.get("id") or h.get("target_id") or "")
            for h in live
            if str(h.get("id") or h.get("target_id") or "")
        }
        gone = await self.gone_ids()
        await self._dismiss_vanished_no_backup_prompts(live_ids)
        covered = await self._covered_backup_ids()
        for host in live:
            kind = str(host.get("kind") or "")
            if kind not in self._guest_kinds():
                continue
            tid = str(host.get("id") or host.get("target_id") or "")
            if not is_live_backup_target(tid, live_ids=live_ids, gone_ids=gone):
                continue
            waiting = await self.store.find_waiting_prompt(tid)
            if tid in covered:
                if waiting and waiting.get("kind") == "no_backup":
                    await self.store.dismiss_scope_prompt(
                        int(waiting["id"]),
                        reason="Backup-Zeitplan ist auf der Backup-Seite angelegt.",
                    )
                continue
            if await self._backup_skip_decided(tid):
                continue
            if waiting:
                continue
            name = str(host.get("name") or tid)
            pid = await self.store.insert_scope_prompt(
                target_id=tid,
                target_name=name,
                kind="no_backup",
                reason=f"{name} hat keinen Backup-Plan. So gewollt?",
            )
            if pid:
                await self._notify_waiting(
                    "Wartet auf dich",
                    f"{name} hat keinen Backup-Plan.",
                )

    async def _schedules_for_host(self, target_id: str) -> list[dict[str, Any]]:
        bstore = self._get_backup_store()
        if bstore is None:
            return []
        try:
            rows = await bstore.list_schedules()
        except Exception:
            return []
        return [
            r
            for r in rows
            if str(r.get("parent_id") or "") == target_id
            and str(r.get("stack") or "")
        ]

    async def _log_unplanned_backup(self, target_id: str, target_name: str) -> None:
        await self._log_activity(
            ACTION_WARN,
            result=RESULT_WAIT,
            kind=KIND_BACKUP,
            target_id=target_id,
            target_name=target_name,
            detail=(
                f"{target_name} hat keinen Backup-Zeitplan. "
                "Bitte auf der Backup-Seite festlegen (Stack und tar/restic) — "
                "der Agent erfindet keine Stacks."
            ),
        )

    async def _plan_backup_for_host(self, target_id: str, target_name: str) -> list[dict[str, Any]]:
        """Only enqueue windows for stacks already saved on the Backup page."""
        if not await self._schedules_for_host(target_id):
            return []
        upserted = await self.ingest()
        return [
            w
            for w in upserted
            if str(w.get("kind") or "") == KIND_BACKUP
            and str(w.get("target_id") or "") == target_id
        ]

    def _job_age_seconds(self, job: Any) -> float:
        created = float(getattr(job, "created_at", 0) or 0)
        if not created:
            return 0.0
        start = datetime.fromtimestamp(created, tz=BERLIN)
        return max(0.0, (now_berlin() - start).total_seconds())

    def _hung_limit_seconds(self, kind: str) -> float:
        if kind in ("apply", "image-apply", "apply-batch"):
            try:
                from patcher.config import get_patcher_settings

                return float(get_patcher_settings().patcher_apply_timeout)
            except Exception:
                return 2 * 3600.0
        return 30 * 60.0

    def _job_is_hung(self, job: Any) -> bool:
        kind = str(getattr(job, "kind", "") or "")
        limit = self._hung_limit_seconds(kind if kind in ("apply", "image-apply") else "backup")
        return self._job_age_seconds(job) > limit

    async def _any_backup_running(self) -> bool:
        """True if a backup *window* is still running. Orphan jobs do not block the chain."""
        for w in await self.store.list_windows(statuses=[STATUS_RUNNING]):
            if str(w.get("kind") or "") == KIND_BACKUP:
                return True
        return False

    async def _backup_skip_reason(
        self,
        row: dict[str, Any],
        *,
        online: dict[str, bool],
        disk: dict[str, bool],
        running: set[str],
    ) -> str | None:
        tid = str(row.get("target_id") or "")
        if tid in await self.gone_ids():
            return REASON_HOST_GONE
        if tid in running:
            return "Auf diesem Ziel läuft bereits ein Auftrag."
        if disk.get(tid):
            return REASON_DEST_FULL
        if not online.get(tid, True):
            return REASON_HOST_OFFLINE_CHAIN
        dest_skip = await self._dest_skip_for_row(row)
        if dest_skip:
            return dest_skip
        return None

    async def _apply_backup_skip(
        self, row: dict[str, Any], reason: str, *, now: datetime
    ) -> None:
        tid = str(row.get("target_id") or "")
        unexpected_offline = reason == REASON_HOST_OFFLINE_CHAIN and (
            REASON_HOST_OFFLINE_CHAIN not in (row.get("gates") or [])
            and (row.get("extra") or {}).get("planned_online", True)
        )
        if reason == REASON_DEST_FULL:
            await self.store.update_window(
                int(row["id"]), status=STATUS_SKIPPED, reason=reason
            )
            await self._record_lesson(
                row, job=None, job_id=None, error="Ziel voll", rollback=None
            )
            await self._log_activity(
                ACTION_SKIPPED,
                result=RESULT_SKIP,
                row=row,
                detail=reason,
            )
        elif reason == REASON_HOST_OFFLINE_CHAIN:
            await self._flag_offline_visible(row, kind_label="Backup")
        else:
            await self.store.update_window(int(row["id"]), reason=by_agent(reason))
            await self._log_activity(
                ACTION_SKIPPED,
                result=RESULT_SKIP,
                row=row,
                detail=reason,
            )
        if unexpected_offline and reason == REASON_HOST_OFFLINE_CHAIN:
            await self._record_lesson(
                row, job=None, job_id=None, error="Host offline", rollback=None
            )
        await self._pack_backup_chain(now=now, start_min=self._chain_cursor(now))
        _ = tid

    def _chain_cursor(self, now: datetime) -> int:
        """Next job starts now — no 10-minute wait after a finished predecessor."""
        return abs_minutes(now, day_start(now))

    async def _pack_backup_chain(
        self, *, now: datetime, start_min: int | None = None
    ) -> list[dict[str, Any]]:
        settings = await self.settings()
        rows = [
            w
            for w in await self.store.list_windows(statuses=[STATUS_ACCEPTED])
            if str(w.get("kind") or "") in (KIND_BACKUP, KIND_PATCH)
            and not is_hard_stop(str(w.get("kind") or ""))
            and not is_hard_stop(str(w.get("bucket") or ""))
        ]
        rows.sort(key=self._accepted_start_order)
        if not rows:
            return []
        origin = day_start(now)
        others = [
            w
            for w in await self.store.list_windows(
                statuses=[STATUS_ACCEPTED, STATUS_WAITING, STATUS_RUNNING]
            )
            if w.get("id") not in {r.get("id") for r in rows}
        ]
        occupied = self._occupied_from_rows(others, now)
        cursor = start_min
        pack = rows
        if cursor is None:
            cursor = self._row_abs(rows[0], origin) + int(
                rows[0].get("duration_min") or DURATION_BACKUP
            )
            pack = rows[1:]
        shifts: list[dict[str, Any]] = []
        for row in pack:
            kind = str(row.get("kind") or KIND_BACKUP)
            dur = int(row.get("duration_min") or DURATION_BACKUP)
            try:
                new_min = next_free_slot(
                    target_id=str(row.get("target_id") or ""),
                    kind=kind,
                    duration_min=dur,
                    occupied=occupied,
                    start_min=cursor,
                    quiet_start=settings.quiet_start,
                    quiet_end=settings.quiet_end,
                    require_quiet=False,
                    grid_min=1,
                )
            except RuntimeError:
                continue
            old_iso = str(row.get("start_iso") or "")
            old_hm = str(row.get("start_hm") or "")
            new_dt = dt_from_abs(now, new_min)
            new_iso = iso_utc(new_dt)
            new_hm = format_hhmm(clock_of(new_min))
            await self.store.update_window(
                int(row["id"]),
                start_iso=new_iso,
                start_hm=new_hm,
                reason=REASON_BACKUP_CHAIN,
            )
            if old_iso != new_iso or old_hm != new_hm:
                await self.store.add_shift(
                    int(row["id"]),
                    old_start_iso=old_iso,
                    old_start_hm=old_hm,
                    new_start_iso=new_iso,
                    new_start_hm=new_hm,
                    reason=REASON_BACKUP_CHAIN,
                )
                await self._log_activity(
                    ACTION_BACKUP_CHAIN,
                    result=RESULT_OK,
                    row=row,
                    detail=f"{old_hm} → {new_hm}",
                )
                rec = {
                    "window_id": int(row["id"]),
                    "old_start_hm": old_hm,
                    "new_start_hm": new_hm,
                    "reason": REASON_BACKUP_CHAIN,
                    "kind": kind,
                    "target_name": row.get("target_name"),
                }
                shifts.append(rec)
            if row.get("schedule_id"):
                await self._rewrite_schedule_time(int(row["schedule_id"]), new_hm)
            occupied.append(
                Occupied(
                    target_id=str(row.get("target_id") or ""),
                    kind=kind,
                    start_min=new_min,
                    duration_min=dur,
                    global_backup=kind in (KIND_BACKUP, KIND_DRILL),
                )
            )
            cursor = new_min + dur
        return shifts

    async def _advance_backup_chain(self) -> None:
        now = now_berlin()
        await self._pack_backup_chain(now=now, start_min=self._chain_cursor(now))
        online, disk, running, _ = await self._host_maps()
        settings = await self.settings()
        patch_busy = any(
            str(getattr(j, "kind", "") or "") in ("apply", "image-apply")
            for j in self._list_patch_jobs()
        )
        rows = [
            w
            for w in await self.store.list_windows(statuses=[STATUS_ACCEPTED])
            if str(w.get("kind") or "") in (KIND_BACKUP, KIND_PATCH)
            and not w.get("needs_confirm")
            and not is_hard_stop(str(w.get("kind") or ""))
            and not is_hard_stop(str(w.get("bucket") or ""))
        ]
        rows.sort(key=self._accepted_start_order)
        for row in rows:
            kind = str(row.get("kind") or "")
            tid = str(row.get("target_id") or "")
            if kind == KIND_BACKUP:
                if await self._any_backup_running():
                    continue
                skip = await self._backup_skip_reason(
                    row, online=online, disk=disk, running=running
                )
                if skip:
                    await self._apply_backup_skip(row, skip, now=now)
                    continue
            elif kind == KIND_PATCH:
                if settings.patch_halted or patch_busy:
                    continue
                if tid in running:
                    continue
            try:
                await self.start_now(int(row["id"]), replan=False)
            except Exception:
                continue
            await self._pack_backup_chain(
                now=now_berlin(), start_min=self._chain_cursor(now_berlin())
            )
            return
        await self._pack_backup_chain(now=now, start_min=self._chain_cursor(now))

    async def _flag_hung_jobs(self) -> None:
        now = now_berlin()
        running = await self.store.list_windows(statuses=[STATUS_RUNNING])
        jobs: list[Any] = []
        jobs.extend(self._list_backup_jobs())
        jobs.extend(self._list_patch_jobs())
        by_id = {str(getattr(j, "id", "")): j for j in jobs}
        for row in running:
            job = by_id.get(str(row.get("job_id") or ""))
            if job is None or not self._job_is_hung(job):
                continue
            if str(row.get("status") or "") == STATUS_WAITING:
                continue
            reason = by_agent(REASON_HUNG)
            await self.store.update_window(
                int(row["id"]),
                status=STATUS_WAITING,
                needs_confirm=True,
                reason=reason,
            )
            await self._log_activity(
                ACTION_WARN,
                result=RESULT_WAIT,
                row=row,
                detail=REASON_HUNG,
            )
            await self._notify_waiting(
                "Wartet auf dich",
                f"{row.get('target_name') or row.get('target_id')}: Auftrag hängt.",
            )

    def _is_kernel_engine_row(self, row: dict[str, Any], job: Any) -> bool:
        names = [n.lower() for n in self._window_packages(row)]
        reasons = [str(r).lower() for r in (row.get("confirm_reasons") or [])]
        if job is not None:
            result = getattr(job, "result", None) or {}
            for extra in result.get("packages") or []:
                if isinstance(extra, str):
                    names.append(extra.lower())
                elif isinstance(extra, dict) and extra.get("name"):
                    names.append(str(extra["name"]).lower())
        tokens = ("kernel", "linux-image", "docker-ce", "docker.io", "containerd")
        if any(any(t in n for t in tokens) for n in names):
            return True
        return any(r in ("kernel", "docker", "kernel-docker") for r in reasons)

    async def _maybe_wait_reboot(self, row: dict[str, Any], job: Any) -> None:
        await self._maybe_reboot_after_apply(row, job)

    def _reboot_needs_confirm(self, row: dict[str, Any], job: Any, policy: ConfirmPolicy) -> bool:
        if policy.confirm_nothing:
            return False
        reasons = list(row.get("confirm_reasons") or [])
        if self._is_kernel_engine_row(row, job):
            reasons = list({*reasons, "kernel", "docker"})
        wait, _why = needs_human(
            policy,
            kind=KIND_PATCH,
            bucket=str(row.get("bucket") or ""),
            confirm_reasons=reasons,
            tags=list((row.get("extra") or {}).get("tags") or row.get("tags") or []),
            known_host=True,
        )
        return bool(wait)

    async def _insert_notice_window(
        self,
        *,
        target_id: str,
        target_name: str,
        bucket: str,
        reason: str,
        kind: str = KIND_PATCH,
        needs_confirm: bool = True,
    ) -> dict[str, Any] | None:
        existing = await self.store.find_open_bucket(target_id=target_id, bucket=bucket)
        if existing:
            return existing
        now = now_berlin()
        wid = await self.store.insert_window(
            {
                "kind": kind,
                "target_id": target_id,
                "target_name": target_name,
                "bucket": bucket,
                "start_iso": iso_utc(now),
                "start_hm": now.strftime("%H:%M"),
                "duration_min": 10,
                "status": STATUS_WAITING if needs_confirm else STATUS_DONE,
                "needs_confirm": needs_confirm,
                "source": SOURCE_AGENT,
                "reason": by_agent(reason),
            }
        )
        return await self.store.get_window(wid)

    async def _maybe_reboot_after_apply(self, row: dict[str, Any], job: Any) -> None:
        result = getattr(job, "result", None) if job is not None else None
        reboot = bool(isinstance(result, dict) and result.get("reboot_required"))
        if not reboot:
            return
        tid = str(row.get("target_id") or "")
        name = str(row.get("target_name") or tid)
        existing = await self.store.find_open_bucket(target_id=tid, bucket="reboot")
        if existing:
            return
        policy = await self.policy()
        if self._reboot_needs_confirm(row, job, policy):
            await self._insert_notice_window(
                target_id=tid,
                target_name=name,
                bucket="reboot",
                reason=REASON_REBOOT_WAIT,
            )
            await self._log_activity(
                ACTION_REBOOT,
                result=RESULT_WAIT,
                row=row,
                detail=REASON_REBOOT_WAIT,
            )
            await self._notify_waiting("Wartet auf dich", f"{name}: Reboot nötig.")
            return
        await self._perform_reboot(row)

    async def _perform_reboot(self, row: dict[str, Any]) -> dict[str, Any]:
        tid = str(row.get("target_id") or "")
        name = str(row.get("target_name") or tid)
        if self._reboot_host is None:
            notice = await self._insert_notice_window(
                target_id=tid,
                target_name=name,
                bucket="reboot",
                reason=REASON_REBOOT_NO_API,
            )
            await self._log_activity(
                ACTION_REBOOT,
                result=RESULT_WAIT,
                row=row,
                detail=REASON_REBOOT_NO_API,
            )
            await self._notify_waiting(
                "Wartet auf dich",
                f"{name}: Reboot nötig, keine API.",
            )
            return notice or row
        try:
            raw = await self._reboot_host(tid)
        except Exception as exc:
            raw = None
            err = getattr(exc, "message", None) or str(exc)
            notice = await self._insert_notice_window(
                target_id=tid,
                target_name=name,
                bucket="reboot",
                reason=f"{REASON_REBOOT_NO_API} {err}",
            )
            await self._log_activity(
                ACTION_REBOOT,
                result=RESULT_FAIL,
                row=row,
                detail=err,
            )
            await self._notify_waiting("Wartet auf dich", f"{name}: Reboot fehlgeschlagen.")
            return notice or row
        if not raw:
            notice = await self._insert_notice_window(
                target_id=tid,
                target_name=name,
                bucket="reboot",
                reason=REASON_REBOOT_NO_API,
            )
            await self._log_activity(
                ACTION_REBOOT,
                result=RESULT_WAIT,
                row=row,
                detail=REASON_REBOOT_NO_API,
            )
            await self._notify_waiting(
                "Wartet auf dich",
                f"{name}: Reboot nötig, keine API.",
            )
            return notice or row
        reason = by_agent(REASON_REBOOT_DONE)
        if row.get("id") and str(row.get("bucket") or "") == "reboot":
            await self.store.update_window(
                int(row["id"]),
                status=STATUS_DONE,
                needs_confirm=False,
                reason=reason,
            )
        else:
            await self._insert_notice_window(
                target_id=tid,
                target_name=name,
                bucket="reboot",
                reason=REASON_REBOOT_DONE,
                needs_confirm=False,
            )
        await self._log_activity(
            ACTION_REBOOT,
            result=RESULT_OK,
            row=row,
            detail=REASON_REBOOT_DONE,
        )
        return await self.store.find_open_bucket(target_id=tid, bucket="reboot") or row

    async def _flag_offline_visible(self, row: dict[str, Any], *, kind_label: str) -> None:
        tid = str(row.get("target_id") or "")
        name = str(row.get("target_name") or tid)
        reason = REASON_OFFLINE_TODAY.format(name=name).replace(
            "Backup/Patch", kind_label
        )
        if str(row.get("status") or "") == STATUS_ACCEPTED:
            await self.store.update_window(
                int(row["id"]),
                status=STATUS_SKIPPED,
                reason=by_agent(reason),
            )
        notice = await self._insert_notice_window(
            target_id=tid,
            target_name=name,
            bucket="offline",
            reason=reason,
            kind=str(row.get("kind") or KIND_BACKUP),
        )
        if not await self.store.has_activity_today(
            action=ACTION_SKIPPED, target_id=tid, detail_contains="offline"
        ):
            await self._log_activity(
                ACTION_SKIPPED,
                result=RESULT_SKIP,
                row=row,
                detail=reason,
            )
            await self._notify_waiting("Wartet auf dich", reason)
        _ = notice

    async def _dest_payload(self) -> dict[str, Any]:
        if self._dest_usage is not None:
            try:
                return await self._dest_usage() or {}
            except Exception:
                return {}
        store = self._get_backup_store()
        if store is None or not hasattr(store, "list_destinations"):
            return {}
        try:
            from app.core.backup_storage import build_backup_storage

            return await build_backup_storage(store)
        except Exception:
            return {}

    async def _estimate_row_bytes(self, row: dict[str, Any]) -> int | None:
        bstore = self._get_backup_store()
        if bstore is None:
            return None
        fn = getattr(bstore, "list_runs_for_stack", None)
        if fn is None:
            return None
        try:
            runs = await fn(
                str(row.get("target_id") or ""),
                str(row.get("stack") or ""),
                limit=8,
            )
        except Exception:
            return None
        return estimate_bytes_from_runs(runs or [])

    async def _dest_skip_for_row(self, row: dict[str, Any]) -> str | None:
        estimate = await self._estimate_row_bytes(row)
        dests = collect_dests(await self._dest_payload())
        for _label, usage in dests:
            if dest_is_critically_full(usage, estimate=estimate):
                return REASON_DEST_FULL
            fits = job_fits(
                usage.get("free_bytes") if usage.get("quota_known") else None,
                estimate,
            )
            if fits is False:
                return REASON_DEST_FULL
        return None

    async def _warn_capacity(self) -> None:
        rows = [
            w
            for w in await self.store.list_windows(statuses=[STATUS_ACCEPTED])
            if str(w.get("kind") or "") == KIND_BACKUP
        ]
        upcoming = 0
        next_job: int | None = None
        for row in rows:
            est = await self._estimate_row_bytes(row)
            if est:
                upcoming += est
                if next_job is None:
                    next_job = est
        dests = collect_dests(await self._dest_payload())
        for line in warn_lines(dests, upcoming_bytes=upcoming, next_job_bytes=next_job):
            if await self.store.has_activity_today(
                action=ACTION_WARN, detail_contains=line[:40]
            ):
                continue
            await self._log_activity(
                ACTION_WARN,
                result=RESULT_INFO,
                kind=KIND_BACKUP,
                detail=line,
            )
            await self._insert_notice_window(
                target_id="dest:capacity",
                target_name="Speicher",
                bucket="capacity",
                reason=f"{REASON_CAPACITY_WARN}: {line}",
            )
            await self._notify_waiting("Wartet auf dich", by_agent(line))

    async def _warn_smart(self) -> None:
        signals: list[dict[str, Any]] = []
        if self._smart_signals is not None:
            try:
                signals = await self._smart_signals() or []
            except Exception:
                signals = []
        else:
            signals = await self._read_smart_signals()
        for item in signals:
            chip = str(item.get("chip") or "").lower()
            health = str(item.get("health") or "").lower()
            hot = chip in ("warn", "danger", "critical") or health in (
                "failed",
                "failing",
                "prefail",
                "pre-fail",
            )
            if not hot:
                continue
            disk = str(item.get("disk") or item.get("name") or "disk")
            node = str(item.get("node") or "")
            tid = f"smart:{node}:{disk}"
            detail = (
                f"{node} {disk}: SMART {item.get('health') or chip} "
                f"— Backups laufen weiter."
            )
            if await self.store.has_activity_today(action=ACTION_WARN, target_id=tid):
                continue
            await self._log_activity(
                ACTION_WARN,
                result=RESULT_INFO,
                target_id=tid,
                target_name=f"{node} {disk}".strip(),
                detail=detail,
            )
            await self._insert_notice_window(
                target_id=tid,
                target_name=f"{node} {disk}".strip() or "SMART",
                bucket="smart",
                reason=f"{REASON_SMART_WARN} {detail}",
            )
            await self._notify_waiting("Wartet auf dich", by_agent(detail))

    async def _read_smart_signals(self) -> list[dict[str, Any]]:
        try:
            from app.main import app as fastapi_app

            engine = getattr(fastapi_app.state, "discovery_engine", None)
            snap = getattr(
                getattr(fastapi_app.state, "topology_store", None), "snapshot", None
            )
        except Exception:
            return []
        if engine is None or snap is None:
            return []
        nodes = list(getattr(snap, "nodes", None) or [])
        out: list[dict[str, Any]] = []
        for node in nodes:
            name = getattr(node, "name", None) or (
                node.get("name") if isinstance(node, dict) else None
            )
            if not name or str(name).startswith("__"):
                continue
            try:
                data = await engine.fetch_node_storage_health(str(name))
            except Exception:
                continue
            for row in data.get("smart") or []:
                if isinstance(row, dict):
                    out.append({**row, "node": str(name)})
        return out

    async def _propose_eol(self) -> None:
        snap = self._get_snapshot()
        try:
            from patcher.module import _get_store as _pstore
            from patcher.targets import list_targets

            pstore = _pstore()
            if pstore is None or snap is None:
                return
            targets = await list_targets(pstore, snap)
        except Exception:
            return
        for target in targets:
            tid = str(getattr(target, "id", "") or "")
            if not tid:
                continue
            try:
                latest = await pstore.latest_scan_for_target(tid)
            except Exception:
                continue
            if str((latest or {}).get("status") or "") != "success":
                continue
            ru = ((latest or {}).get("summary") or {}).get("release_upgrade") or {}
            if not isinstance(ru, dict):
                continue
            headline = str(ru.get("headline") or ru.get("title") or "").strip()
            if not headline:
                continue
            if await self.store.find_open_bucket(target_id=tid, bucket="eol"):
                continue
            if await self.store.has_activity_today(action=ACTION_WARN, target_id=tid, detail_contains="EOL"):
                continue
            name = str(getattr(target, "name", None) or tid)
            reason = f"{REASON_EOL_PROPOSE} {headline}"
            await self._insert_notice_window(
                target_id=tid,
                target_name=name,
                bucket="eol",
                reason=reason,
            )
            await self._log_activity(
                ACTION_WARN,
                result=RESULT_WAIT,
                kind=KIND_PATCH,
                target_id=tid,
                target_name=name,
                detail=reason,
            )
            await self._notify_waiting("Wartet auf dich", f"{name}: {headline}")

    async def _maybe_prune_after_image(self, row: dict[str, Any]) -> None:
        tid = str(row.get("target_id") or "")
        if not tid:
            return
        open_img = [
            w
            for w in await self.store.list_windows(
                statuses=[STATUS_ACCEPTED, STATUS_WAITING, STATUS_RUNNING]
            )
            if self._is_image_window(w) and str(w.get("target_id") or "") == tid
        ]
        if open_img:
            return
        today = now_berlin().date().isoformat()
        failed = [
            w
            for w in await self.store.list_windows(statuses=[STATUS_FAILED])
            if self._is_image_window(w)
            and str(w.get("target_id") or "") == tid
            and str(w.get("updated_at_iso") or w.get("start_iso") or "").startswith(today)
        ]
        if failed:
            await self._log_activity(
                ACTION_PRUNE,
                result=RESULT_SKIP,
                row=row,
                kind="image",
                detail="Image fehlgeschlagen — kein Prune.",
            )
            return
        if await self.store.has_activity_today(
            action=ACTION_PRUNE, target_id=tid, detail_contains="bereinigt"
        ):
            return
        if self._prune_images is None:
            return
        try:
            raw = await self._prune_images(tid)
        except Exception as exc:
            await self._log_activity(
                ACTION_PRUNE,
                result=RESULT_FAIL,
                row=row,
                kind="image",
                detail=str(exc),
            )
            return
        if not raw:
            return
        msg = str(raw.get("message") or REASON_PRUNE)
        await self._log_activity(
            ACTION_PRUNE,
            result=RESULT_OK,
            row=row,
            kind="image",
            detail=msg,
        )

    async def _copilot_backup_stack(self) -> dict[str, Any] | None:
        snap = self._get_snapshot()
        if snap is None:
            return None
        try:
            stacks = await self._list_backup_stacks(snap)
        except Exception:
            return None
        for row in stacks or []:
            stack = str(row.get("stack") or "").lower()
            if any(hint in stack for hint in COPILOT_STACK_HINTS):
                return row
        return None

    async def _copilot_already_planned(self) -> bool:
        if await self.store.find_open_for_target(
            kind=KIND_BACKUP, target_id=COPILOT_DATA_ID, stack=""
        ):
            return True
        for w in await self.store.list_windows(
            statuses=[STATUS_ACCEPTED, STATUS_WAITING, STATUS_RUNNING]
        ):
            if str(w.get("kind") or "") != KIND_BACKUP:
                continue
            stack = str(w.get("stack") or "").lower()
            tid = str(w.get("target_id") or "")
            if tid == COPILOT_DATA_ID or any(h in stack for h in COPILOT_STACK_HINTS):
                return True
        bstore = self._get_backup_store()
        if bstore is None:
            return False
        try:
            for row in await bstore.list_schedules():
                stack = str(row.get("stack") or "").lower()
                pid = str(row.get("parent_id") or "")
                if pid == COPILOT_DATA_ID or any(h in stack for h in COPILOT_STACK_HINTS):
                    return True
        except Exception:
            return False
        return False

    async def _forget_synthetic_copilot_host(self) -> None:
        """Drop the ghost Copilot-/data inventory row; keep a dedicated prompt if needed."""
        for host in await self.store.list_known_hosts():
            if is_synthetic_copilot_data(str(host.get("target_id") or "")):
                await self.store.delete_known_host(COPILOT_DATA_ID)
                break
        for prompt in await self.store.list_scope_prompts(status="waiting"):
            if not is_synthetic_copilot_data(str(prompt.get("target_id") or "")):
                continue
            if prompt.get("kind") in ("appeared", "disappeared"):
                await self.store.dismiss_scope_prompt(
                    int(prompt["id"]),
                    reason="Copilot /data ist kein Host — Frage zurückgezogen.",
                )

    async def _backup_skip_decided(self, target_id: str) -> bool:
        tid = str(target_id or "").strip()
        if not tid:
            return False
        for host in await self.store.list_known_hosts():
            if str(host.get("target_id") or "") == tid and host.get("skip_backup") is not None:
                return True
        for prompt in await self.store.list_scope_prompts(status="answered"):
            if (
                str(prompt.get("target_id") or "") == tid
                and prompt.get("kind") == "no_backup"
                and prompt.get("backup") is not None
            ):
                return True
        return False

    async def _dismiss_vanished_no_backup_prompts(self, live_ids: set[str]) -> None:
        first_class = bool(await self._copilot_backup_stack())
        planned = await self._copilot_already_planned()
        for prompt in await self.store.list_scope_prompts(status="waiting"):
            if prompt.get("kind") != "no_backup":
                continue
            tid = str(prompt.get("target_id") or "")
            if not tid:
                continue
            if is_synthetic_copilot_data(tid):
                if first_class and not planned:
                    continue
                await self.store.dismiss_scope_prompt(
                    int(prompt["id"]),
                    reason="Copilot /data ist kein Host — Frage zurückgezogen.",
                )
                continue
            if tid in live_ids:
                continue
            await self.store.dismiss_scope_prompt(
                int(prompt["id"]),
                reason="Host ist weggefallen — Backup-Frage entfällt.",
            )

    async def _prompt_copilot_data(self) -> None:
        if await self._backup_skip_decided(COPILOT_DATA_ID):
            return
        if await self._copilot_already_planned():
            waiting = await self.store.find_waiting_prompt(COPILOT_DATA_ID)
            if waiting and waiting.get("kind") == "no_backup":
                await self.store.dismiss_scope_prompt(
                    int(waiting["id"]),
                    reason="Backup-Zeitplan ist auf der Backup-Seite angelegt.",
                )
            return
        if not await self._copilot_backup_stack():
            waiting = await self.store.find_waiting_prompt(COPILOT_DATA_ID)
            if waiting and waiting.get("kind") == "no_backup":
                await self.store.dismiss_scope_prompt(
                    int(waiting["id"]),
                    reason="Copilot /data ist kein Host — Frage zurückgezogen.",
                )
            return
        waiting = await self.store.find_waiting_prompt(COPILOT_DATA_ID)
        if waiting:
            return
        pid = await self.store.insert_scope_prompt(
            target_id=COPILOT_DATA_ID,
            target_name="Copilot /data",
            kind="no_backup",
            reason=REASON_COPILOT_DATA,
        )
        if pid:
            await self._log_activity(
                ACTION_WARN,
                result=RESULT_WAIT,
                kind=KIND_BACKUP,
                target_id=COPILOT_DATA_ID,
                target_name="Copilot /data",
                detail=REASON_COPILOT_DATA,
            )
            await self._notify_waiting("Wartet auf dich", REASON_COPILOT_DATA)

    async def _plan_copilot_data_if_possible(self) -> list[dict[str, Any]]:
        bstore = self._get_backup_store()
        if bstore is None:
            return []
        try:
            for row in await bstore.list_schedules():
                stack = str(row.get("stack") or "").lower()
                pid = str(row.get("parent_id") or "")
                if pid == COPILOT_DATA_ID or any(
                    h in stack for h in COPILOT_STACK_HINTS
                ):
                    tid = pid or COPILOT_DATA_ID
                    name = str(row.get("guest_name") or "Copilot /data")
                    return await self._plan_backup_for_host(tid, name)
        except Exception:
            return []
        return []

    async def refresh_evening_brief(self, *, force: bool = False) -> dict[str, Any]:
        now = now_berlin()
        day = now.date().isoformat()
        existing = await self.store.get_brief_for_day(day)
        log = await self.store.list_activity(limit=300)
        live_ids = {str(h["id"]) for h in await self._live_hosts() if h.get("id")}
        if (
            await self._copilot_backup_stack()
            and not await self._copilot_already_planned()
            and not await self._backup_skip_decided(COPILOT_DATA_ID)
        ):
            live_ids.add(COPILOT_DATA_ID)
        gone = await self.gone_ids()
        if COPILOT_DATA_ID not in live_ids:
            gone.add(COPILOT_DATA_ID)
        text = build_evening_brief(log, now=now, gone_ids=gone, live_ids=live_ids)
        if existing and existing.get("text") == text and not force:
            return existing
        saved = await self.store.save_brief(day=day, text=text)
        await self._log_activity(
            ACTION_BRIEF,
            result=RESULT_OK,
            detail=text,
        )
        return saved

    async def _maybe_refresh_brief(self) -> None:
        leftover = [
            w
            for w in await self.store.list_windows(
                statuses=[STATUS_ACCEPTED, STATUS_RUNNING]
            )
            if str(w.get("kind") or "") == KIND_BACKUP
        ]
        if leftover:
            return
        now = now_berlin()
        settings = await self.settings()
        hm = now.strftime("%H:%M")
        if hm < str(settings.quiet_start or "20:00"):
            log = await self.store.list_activity(limit=20)
            if not any(
                r.get("action") in (ACTION_APPLY, ACTION_REBOOT, ACTION_SKIPPED)
                for r in log
            ):
                return
        await self.refresh_evening_brief()

    async def _proactive_checks(self) -> None:
        now = now_berlin().timestamp()
        if now - self._last_proactive < 120:
            return
        self._last_proactive = now
        try:
            await self._warn_capacity()
        except Exception:
            logger.info("Kapazitätswarnung fehlgeschlagen", exc_info=True)
        try:
            await self._warn_smart()
        except Exception:
            logger.info("SMART-Warnung fehlgeschlagen", exc_info=True)
        try:
            await self._propose_eol()
        except Exception:
            logger.info("EOL-Vorschlag fehlgeschlagen", exc_info=True)

    async def _notify_waiting(self, title: str, body: str) -> None:
        if self._notify_shift is None:
            return
        try:
            await self._notify_shift(title, body)
        except Exception:
            logger.info("Push für Wartet-auf-dich fehlgeschlagen", exc_info=True)

    def _accepted_start_order(self, row: dict[str, Any]) -> tuple[int, str, int]:
        kind = str(row.get("kind") or "")
        rank = 0 if kind == KIND_BACKUP else 1
        return (rank, str(row.get("start_iso") or ""), int(row.get("id") or 0))

    async def _mark_schedule_used(self, row: dict[str, Any]) -> None:
        """Skip tonight's ingested cron so start-now does not double-fire."""
        sid = row.get("schedule_id")
        if not sid:
            return
        bstore = self._get_backup_store()
        mark = getattr(bstore, "mark_schedule_fired", None)
        if bstore is None or mark is None:
            return
        start = self._parse_start(row) or now_berlin()
        try:
            await mark(int(sid), minute_key=minute_key(start))
        except Exception:
            logger.info("Zeitplan nach Sofort-Start nicht markiert", exc_info=True)

    async def start_now(
        self, window_id: int, *, replan: bool = True, respect_lessons: bool = True
    ) -> dict[str, Any]:
        row = await self.store.get_window(window_id)
        if not row:
            raise RuntimeError("Fenster nicht gefunden.")
        if row.get("kind") == KIND_DRILL:
            raise RuntimeError("Restore-Drill startet der bestehende Drill-Scheduler.")
        kind = str(row.get("kind") or "")
        bucket = str(row.get("bucket") or "")
        if is_hard_stop(kind) or is_hard_stop(bucket):
            raise RuntimeError("Harter Stopp — startet der Agent nie.")
        if bucket == "reboot":
            return await self._perform_reboot(row)
        if bucket in ("offline", "eol", "capacity", "smart"):
            raise RuntimeError("Nur zur Kenntnis — kein Start.")
        online, disk, running, _no_snap = await self._host_maps()
        tid = str(row.get("target_id") or "")
        if not online.get(tid, True):
            raise RuntimeError("Host ist nicht online.")
        if disk.get(tid):
            raise RuntimeError("Disk ist kritisch — Start blockiert.")
        if tid in running:
            raise RuntimeError("Auf diesem Ziel läuft bereits ein Auftrag.")
        if kind == KIND_BACKUP and await self._any_backup_running():
            raise RuntimeError("Ein Backup läuft bereits — der Agent startet das nächste im Anschluss.")
        if kind == KIND_PATCH:
            if not in_job_scope(
                await self.policy(),
                kind=kind,
                bucket=bucket,
                target_id=tid,
                gone_ids=await self.gone_ids(),
            ):
                await self.store.update_window(
                    window_id, status=STATUS_SKIPPED, reason=REASON_OUT_OF_FOCUS
                )
                raise RuntimeError(REASON_OUT_OF_FOCUS)
            if respect_lessons:
                hold = await self._hold_for_lesson(row)
                if hold:
                    raise RuntimeError(hold)
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
            await self._note_scan_on_job(row, job_id)
            await self._log_activity(
                ACTION_STARTED,
                result=RESULT_OK,
                row=row,
                kind="image" if self._is_image_window(row) else KIND_PATCH,
                detail="Apply gestartet",
            )
        elif kind == KIND_BACKUP:
            job_id = await self._start_backup(row)
            await self.store.update_window(window_id, status=STATUS_RUNNING, job_id=job_id)
            await self._mark_schedule_used(row)
            await self._log_activity(
                ACTION_STARTED,
                result=RESULT_OK,
                row=row,
                detail="Backup gestartet",
            )
        else:
            raise RuntimeError("Dieser Fenstertyp lässt sich hier nicht starten.")
        if replan:
            await self.watch_and_shift(force=True)
        result = await self.store.get_window(window_id)
        assert result is not None
        return result

    async def start_accepted_now(self) -> dict[str, Any]:
        """Start accepted upcoming windows now (same gates as slot time, no 20:00 wait)."""
        blocked: list[str] = []
        windows: list[dict[str, Any]] = []
        for w in await self.store.list_windows(statuses=[STATUS_ACCEPTED]):
            kind = str(w.get("kind") or "")
            bucket = str(w.get("bucket") or "")
            if kind == KIND_DRILL or is_hard_stop(kind) or is_hard_stop(bucket):
                blocked.append(kind or bucket or "stop")
                continue
            if w.get("needs_confirm"):
                blocked.append("confirm")
                continue
            windows.append(w)
        windows.sort(key=self._accepted_start_order)
        await self._warn_capacity()
        started: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        patch_started = False
        settings = await self.settings()
        online, disk, running, _no_snap = await self._host_maps()
        backup_started = await self._any_backup_running()
        if any(
            str(getattr(j, "kind", "") or "") in ("apply", "image-apply")
            for j in self._list_patch_jobs()
        ):
            patch_started = True

        for row in windows:
            wid = int(row["id"])
            tid = str(row.get("target_id") or "")
            kind = str(row.get("kind") or "")
            if kind == KIND_BACKUP:
                if backup_started:
                    skipped.append(
                        {"id": wid, "reason": "Ein Backup läuft — Anschluss folgt."}
                    )
                    continue
                skip = await self._backup_skip_reason(
                    row, online=online, disk=disk, running=running
                )
                if skip:
                    await self._apply_backup_skip(row, skip, now=now_berlin())
                    skipped.append({"id": wid, "reason": skip})
                    continue
            if kind == KIND_PATCH:
                if settings.patch_halted:
                    skipped.append({"id": wid, "reason": "Patch-Welle ist gestoppt."})
                    continue
                if patch_started:
                    skipped.append(
                        {"id": wid, "reason": "Ein Apply läuft bereits — ein Host nach dem anderen."}
                    )
                    continue
                if tid in running:
                    skipped.append(
                        {
                            "id": wid,
                            "reason": "Backup, Restore oder Apply läuft bereits auf diesem Ziel.",
                        }
                    )
                    continue
            try:
                result = await self.start_now(wid, replan=False)
            except Exception as exc:
                skipped.append(
                    {"id": wid, "reason": str(exc) or exc.__class__.__name__}
                )
                continue
            started.append(result)
            if tid:
                running.add(tid)
            if kind == KIND_PATCH:
                patch_started = True
            if kind == KIND_BACKUP:
                backup_started = True

        shifts: list[dict[str, Any]] = []
        try:
            now = now_berlin()
            chain_shifts = await self._pack_backup_chain(
                now=now, start_min=self._chain_cursor(now)
            )
            later = await self.watch_and_shift(force=True)
            shifts = list(chain_shifts) + list(later)
        except Exception:
            logger.exception("Nach Sofort-Start nicht neu geplant")
        message = self._start_now_message(started, skipped, shifts, blocked=blocked)
        await self._log_activity(
            ACTION_STARTED,
            result=RESULT_OK if started else RESULT_SKIP,
            detail=message,
        )
        return {
            "ok": True,
            "started": started,
            "skipped": skipped,
            "shifted": shifts,
            "message": message,
        }

    def _start_now_message(
        self,
        started: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        shifts: list[dict[str, Any]],
        *,
        blocked: list[str] | None = None,
    ) -> str:
        names: list[str] = []
        for row in started:
            label = str(row.get("target_name") or row.get("target_id") or "Fenster")
            stack = str(row.get("stack") or "")
            kind = "Backup" if str(row.get("kind") or "") == KIND_BACKUP else "Patch"
            bit = f"{label} · {stack}" if stack else label
            names.append(f"{bit} ({kind})")
        if not started:
            reasons: list[str] = []
            seen: set[str] = set()
            for item in skipped:
                reason = str(item.get("reason") or "").strip()
                if not reason or reason in seen:
                    continue
                seen.add(reason)
                reasons.append(reason)
                if len(reasons) >= 3:
                    break
            if reasons:
                return "Nichts gestartet — " + "; ".join(reasons)
            kinds = {str(k) for k in (blocked or [])}
            if kinds and kinds <= {KIND_DRILL, "drill"}:
                return (
                    "Nichts gestartet — der Restore-Drill in „Als Nächstes“ "
                    "läuft über den bestehenden Scheduler, nicht über diesen Knopf."
                )
            if kinds:
                return (
                    "Nichts gestartet — die Fenster in „Als Nächstes“ darf der "
                    "Agent nicht selbst starten (Drill, harter Stopp oder Bestätigung)."
                )
            return (
                "Nichts gestartet — in „Als Nächstes“ liegt kein übernommenes "
                "Fenster, das der Agent jetzt anfassen darf."
            )
        msg = f"{len(started)} gestartet: " + ", ".join(names[:4])
        if len(names) > 4:
            msg += " …"
        msg += " — steht unter Läuft."
        if shifts:
            msg += f" {len(shifts)} später verschoben."
        return msg

    async def confirm_window(self, window_id: int) -> dict[str, Any]:
        row = await self.store.get_window(window_id)
        if not row:
            raise RuntimeError("Fenster nicht gefunden.")
        bucket = str(row.get("bucket") or "")
        if bucket == "reboot":
            return await self._perform_reboot(row)
        if bucket in ("offline", "eol", "capacity", "smart"):
            await self.store.update_window(
                window_id,
                status=STATUS_DONE,
                needs_confirm=False,
                reason=by_agent("Zur Kenntnis genommen."),
            )
            result = await self.store.get_window(window_id)
            assert result is not None
            return result
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
            return await self.start_now(window_id, respect_lessons=False)
        result = await self.store.get_window(window_id)
        assert result is not None
        return result

    async def decline_window(self, window_id: int) -> dict[str, Any]:
        row = await self.store.get_window(window_id)
        if not row:
            raise RuntimeError("Fenster nicht gefunden.")
        was_running = str(row.get("status") or "") == STATUS_RUNNING
        reason = (
            by_agent("Von dir beendet — Kette geht weiter.")
            if was_running
            else "Abgelehnt."
        )
        await self.store.update_window(
            window_id,
            status=STATUS_SKIPPED,
            needs_confirm=False,
            reason=reason,
        )
        await self._log_activity(
            ACTION_SKIPPED,
            result=RESULT_SKIP,
            row=row,
            detail=reason,
        )
        if was_running:
            try:
                await self._advance_backup_chain()
            except Exception:
                logger.exception("Kette nach Loslassen nicht fortgesetzt")
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
            try:
                await self._proactive_checks()
            except Exception:
                logger.exception("Proaktive Agent-Checks fehlgeschlagen")
            try:
                await self._maybe_refresh_brief()
            except Exception:
                logger.info("Abend-Kurzlage nicht aktualisiert", exc_info=True)

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
        lessons = await self.store.list_lessons(limit=40)
        rb_by_window = {
            int(r["window_id"]): r
            for r in rollbacks
            if r.get("window_id") is not None
        }
        lesson_by_window = {
            int(r["window_id"]): r
            for r in lessons
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
                lesson=lesson_by_window.get(int(wid)) if wid is not None else None,
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
        activity = [serialize_activity(r) for r in await self.store.list_activity(limit=80)]
        brief = await self.store.get_brief_for_day(now.date().isoformat())
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
            "running": self._merge_live_into_running(running, live_backup, live_patch),
            "waiting": waiting,
            "rollback_failed": [e for e in rollback_events if e.get("status") == "failed"],
            "shifted": shifted,
            "rollbacks": rollback_events,
            "lessons": [serialize_lesson(r) for r in lessons],
            "activity": activity,
            "brief": (brief or {}).get("text") or "",
            "brief_at": (brief or {}).get("created_at") or "",
            "done": done[:40],
            "live_jobs": {"backup": live_backup, "patch": live_patch},
            "time": format_de(now),
            "timezone": "Europe/Berlin",
        }

    def _card_from_live_job(self, raw: dict[str, Any], *, kind: str) -> dict[str, Any]:
        tid = str(raw.get("parent_id") or raw.get("target_id") or "")
        stack = str(raw.get("project") or "")
        name = stack or tid or "Auftrag"
        phase = str(raw.get("phase") or raw.get("message") or "läuft")
        pct = raw.get("percent")
        via = bool(raw.get("via_agent"))
        return {
            "id": f"live:{raw.get('id') or raw.get('job_id')}",
            "kind": kind,
            "kind_label": "Backup" if kind == KIND_BACKUP else "Patch",
            "target_id": tid,
            "target_name": name,
            "stack": stack,
            "status": STATUS_RUNNING,
            "job_id": raw.get("id") or raw.get("job_id"),
            "reason": phase,
            "start_de": phase,
            "duration_label": f"{int(pct)} %" if pct is not None else "",
            "bucket_label": phase,
            **actor_fields(via_agent=via),
        }

    def _merge_live_into_running(
        self,
        running: list[dict[str, Any]],
        live_backup: list[dict[str, Any]],
        live_patch: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        seen = {str(row.get("job_id") or "") for row in running if row.get("job_id")}
        extra: list[dict[str, Any]] = []
        for raw, kind in [(j, KIND_BACKUP) for j in live_backup] + [
            (j, KIND_PATCH) for j in live_patch
        ]:
            jid = str(raw.get("id") or raw.get("job_id") or "")
            st = str(raw.get("status") or "")
            if not jid or jid in seen or st not in ("queued", "running"):
                continue
            extra.append(self._card_from_live_job(raw, kind=kind))
            seen.add(jid)
        return extra + running

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
        lesson: dict[str, Any] | None = None,
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
            "reboot": "Reboot",
            "offline": "Offline",
            "eol": "EOL",
            "capacity": "Speicher",
            "smart": "SMART",
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
        if lesson:
            out["lesson"] = serialize_lesson(lesson)
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
