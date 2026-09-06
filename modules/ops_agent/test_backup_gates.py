"""Missing-backup prompt, backup chain, capacity/offline/hung/reboot — ohne Proxmox."""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.core.locale import BERLIN, iso_utc
from ops_agent.engine import OpsEngine, _schedule_method_fields
from ops_agent.activity import ACTION_REBOOT, ACTION_SKIPPED, ACTION_WARN
from ops_agent.hosts import COPILOT_DATA_ID
from ops_agent.capacity import estimate_bytes_from_runs
from patcher.agent import HostPending
from ops_agent.planner import (
    KIND_BACKUP,
    KIND_PATCH,
    REASON_BACKUP_CHAIN,
    REASON_DEST_FULL,
    REASON_HUNG,
    REASON_OFFLINE_TODAY,
    REASON_REBOOT_DONE,
    REASON_REBOOT_WAIT,
    SOURCE_AGENT,
    SOURCE_INGESTED,
    STATUS_ACCEPTED,
    STATUS_DONE,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_WAITING,
)
from ops_agent.store import OpsStore


class _BakJob:
    def __init__(
        self,
        job_id: str,
        parent_id: str,
        *,
        created_at: float | None = None,
        kind: str = "backup",
        status: str = "running",
        project: str = "paperless",
    ) -> None:
        self.id = job_id
        self.parent_id = parent_id
        self.kind = kind
        self.status = status
        self.project = project
        self.created_at = created_at if created_at is not None else time.time()
        self.result: dict | None = None


class _Ent:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


class _Snap:
    def __init__(self, *, disk: float | None = None, online: bool = True) -> None:
        status = "running" if online else "stopped"
        meta = {"disk_pct": disk} if disk is not None else {}
        self.guests = [
            _Ent(
                id="lxc:pve:1",
                kind="lxc",
                name="mail",
                hostname="mail",
                status=status,
                node="pve",
                meta=meta,
            ),
        ]
        self.hosts = []


def _now() -> datetime:
    return datetime(2026, 9, 6, 18, 0, tzinfo=BERLIN)


class BackupGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = OpsStore(Path(self.tmp.name) / "ops.db")
        await self.store.connect()
        self.bak_jobs: list[_BakJob] = []
        self.started_backup: list[str] = []
        self.started_patch: list[str] = []
        self.notices: list[tuple[str, str]] = []
        self.reboots: list[str] = []
        self.dest: dict = {}
        self.snap = _Snap()
        self.engine = self._engine()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.tmp.cleanup()

    def _engine(self) -> OpsEngine:
        async def _empty(*_a: object, **_k: object) -> list:
            return []

        async def _start_backup(row: dict) -> str | None:
            tid = str(row.get("target_id") or "")
            self.started_backup.append(tid)
            jid = f"b-{len(self.started_backup)}"
            self.bak_jobs.append(_BakJob(jid, tid, created_at=time.time()))
            return jid

        async def _start_patch(row: dict) -> tuple[bool, str, str | None]:
            tid = str(row.get("target_id") or "")
            self.started_patch.append(tid)
            return True, "", f"p-{len(self.started_patch)}"

        async def _notify(title: str, body: str) -> None:
            self.notices.append((title, body))

        async def _reboot(tid: str) -> dict:
            self.reboots.append(tid)
            return {"ok": True, "message": "Reboot"}

        async def _dest() -> dict:
            return dict(self.dest)

        return OpsEngine(
            self.store,
            get_snapshot=lambda: self.snap,
            get_backup_store=lambda: None,
            list_backup_stacks=_empty,
            hosts_from_store=_empty,
            start_backup=_start_backup,
            start_patch=_start_patch,
            list_backup_jobs=lambda: list(self.bak_jobs),
            list_patch_jobs=lambda: [],
            notify_shift=_notify,
            reboot_host=_reboot,
            dest_usage=_dest,
        )

    async def _win(self, *, kind: str, target_id: str, hm: str, **extra: object) -> dict:
        hour, minute = (int(x) for x in hm.split(":"))
        start = _now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        payload = {
            "kind": kind,
            "target_id": target_id,
            "target_name": target_id,
            "stack": extra.pop("stack", "paperless") if kind == KIND_BACKUP else "",
            "bucket": extra.pop("bucket", kind),
            "start_iso": iso_utc(start),
            "start_hm": hm,
            "duration_min": 10,
            "status": extra.pop("status", STATUS_ACCEPTED),
            "source": SOURCE_AGENT,
            "reason": "Test",
            **extra,
        }
        wid = await self.store.insert_window(payload)
        row = await self.store.get_window(wid)
        assert row is not None
        return row

    async def test_missing_backup_prompt_and_kein_backup_sticks(self) -> None:
        await self.store.seed_known_hosts(
            [{"id": "lxc:pve:1", "name": "mail", "kind": "lxc"}]
        )
        await self.engine._prompt_missing_backups(
            [{"id": "lxc:pve:1", "name": "mail", "kind": "lxc"}]
        )
        prompts = await self.store.list_scope_prompts(status="waiting")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["kind"], "no_backup")
        self.assertIn("keinen Backup-Plan", prompts[0]["reason"])
        await self.engine.answer_host_prompt(int(prompts[0]["id"]), backup=False)
        self.assertTrue(
            (await self.store.list_known_hosts())[0]["skip_backup"]
        )
        await self.engine._prompt_missing_backups(
            [{"id": "lxc:pve:1", "name": "mail", "kind": "lxc"}]
        )
        self.assertEqual(await self.store.list_scope_prompts(status="waiting"), [])

    async def test_chain_starts_one_and_shifts_rest(self) -> None:
        a = await self._win(kind=KIND_BACKUP, target_id="lxc:a", hm="20:00", stack="one")
        b = await self._win(kind=KIND_BACKUP, target_id="lxc:b", hm="21:20", stack="two")
        result = await self.engine.start_accepted_now()
        self.assertEqual(self.started_backup, ["lxc:a"])
        self.assertEqual(len(result["started"]), 1)
        later = await self.store.get_window(int(b["id"]))
        assert later is not None
        self.assertEqual(later["reason"], REASON_BACKUP_CHAIN)
        self.assertNotEqual(later["start_hm"], "21:20")
        first = await self.store.get_window(int(a["id"]))
        assert first is not None
        self.assertEqual(first["status"], STATUS_RUNNING)

    async def test_finish_starts_next_immediately(self) -> None:
        first = await self._win(
            kind=KIND_BACKUP,
            target_id="lxc:a",
            hm="18:00",
            stack="one",
            status=STATUS_RUNNING,
            job_id="done1",
        )
        nxt = await self._win(kind=KIND_BACKUP, target_id="lxc:b", hm="20:00", stack="two")
        self.bak_jobs.append(_BakJob("done1", "lxc:a", status="success", project="one"))
        await self.engine.sync_jobs()
        self.assertEqual(self.started_backup, ["lxc:b"])
        later = await self.store.get_window(int(nxt["id"]))
        assert later is not None
        self.assertEqual(later["status"], STATUS_RUNNING)
        self.assertNotEqual(later["start_hm"], "20:00")
        done = await self.store.get_window(int(first["id"]))
        assert done is not None
        self.assertEqual(done["status"], STATUS_DONE)

    async def test_finish_starts_next_patch_without_ten_minute_gap(self) -> None:
        policy = await self.store.get_policy()
        policy.patch_scope_ids = ["lxc:p"]
        await self.store.save_policy(policy)
        await self._win(
            kind=KIND_BACKUP,
            target_id="lxc:a",
            hm="18:00",
            stack="one",
            status=STATUS_RUNNING,
            job_id="done1",
        )
        patch = await self._win(
            kind=KIND_PATCH, target_id="lxc:p", hm="21:00", bucket="security"
        )
        self.bak_jobs.append(_BakJob("done1", "lxc:a", status="success", project="one"))
        await self.engine.sync_jobs()
        self.assertEqual(self.started_patch, ["lxc:p"])
        moved = await self.store.get_window(int(patch["id"]))
        assert moved is not None
        self.assertEqual(moved["status"], STATUS_RUNNING)
        self.assertNotEqual(moved["start_hm"], "21:00")

    async def test_capacity_skips_and_shifts_chain(self) -> None:
        self.snap = _Snap(disk=96)
        self.engine = self._engine()
        full = await self._win(kind=KIND_BACKUP, target_id="lxc:pve:1", hm="20:00")
        nxt = await self._win(kind=KIND_BACKUP, target_id="lxc:b", hm="21:20", stack="other")
        result = await self.engine.start_accepted_now()
        self.assertEqual(self.started_backup, ["lxc:b"])
        skipped = await self.store.get_window(int(full["id"]))
        assert skipped is not None
        self.assertEqual(skipped["status"], STATUS_SKIPPED)
        self.assertEqual(skipped["reason"], REASON_DEST_FULL)
        lessons = await self.store.list_lessons()
        self.assertTrue(lessons)
        later = await self.store.get_window(int(nxt["id"]))
        assert later is not None
        self.assertEqual(later["status"], STATUS_RUNNING)
        self.assertTrue(result["started"])

    async def test_offline_defers_and_continues(self) -> None:
        self.snap = _Snap(online=False)
        self.engine = self._engine()
        down = await self._win(kind=KIND_BACKUP, target_id="lxc:pve:1", hm="20:00")
        nxt = await self._win(kind=KIND_BACKUP, target_id="lxc:ok", hm="20:40", stack="ok")
        await self.engine.start_accepted_now()
        self.assertEqual(self.started_backup, ["lxc:ok"])
        left = await self.store.get_window(int(down["id"]))
        assert left is not None
        self.assertEqual(left["status"], STATUS_SKIPPED)
        self.assertIn("offline", left["reason"].lower())
        self.assertIn("fällt auf", left["reason"])
        waiting = await self.store.list_windows(statuses=[STATUS_WAITING])
        self.assertTrue(any(w.get("bucket") == "offline" for w in waiting))
        self.assertTrue(any("fällt auf" in (t + b) for t, b in self.notices))

    async def test_release_running_window_unblocks_chain(self) -> None:
        stuck = await self._win(
            kind=KIND_BACKUP,
            target_id="lxc:a",
            hm="18:00",
            stack="portaineragent",
            status=STATUS_RUNNING,
            job_id="stuck",
        )
        nxt = await self._win(kind=KIND_BACKUP, target_id="lxc:b", hm="20:00", stack="two")
        self.bak_jobs.append(
            _BakJob("stuck", "lxc:a", status="running", project="portaineragent")
        )
        released = await self.engine.decline_window(int(stuck["id"]))
        self.assertEqual(released["status"], STATUS_SKIPPED)
        self.assertIn("beendet", released["reason"])
        self.assertEqual(self.started_backup, ["lxc:b"])
        later = await self.store.get_window(int(nxt["id"]))
        assert later is not None
        self.assertEqual(later["status"], STATUS_RUNNING)

    async def test_hung_backup_goes_to_waiting(self) -> None:
        row = await self._win(
            kind=KIND_BACKUP,
            target_id="lxc:a",
            hm="18:00",
            status=STATUS_RUNNING,
            job_id="old",
        )
        self.bak_jobs.append(
            _BakJob("old", "lxc:a", created_at=time.time() - 4 * 3600)
        )
        await self.engine._flag_hung_jobs()
        hung = await self.store.get_window(int(row["id"]))
        assert hung is not None
        self.assertEqual(hung["status"], STATUS_WAITING)
        self.assertIn("hängt", hung["reason"])
        self.assertTrue(self.notices)

    async def test_reboot_wait_after_kernel_success(self) -> None:
        job = _BakJob("p1", "lxc:a")
        job.kind = "apply"
        job.status = "success"
        job.result = {"reboot_required": True, "packages": ["linux-image-6.8"]}
        row = await self._win(
            kind=KIND_PATCH,
            target_id="lxc:a",
            hm="20:00",
            bucket="regular",
            status=STATUS_DONE,
            packages=["linux-image-6.8"],
        )
        await self.engine._maybe_wait_reboot(row, job)
        waiting = await self.store.list_windows(statuses=[STATUS_WAITING])
        self.assertTrue(waiting)
        self.assertEqual(waiting[0]["bucket"], "reboot")
        self.assertIn("Reboot", waiting[0]["reason"])
        self.assertIn("durch Agent", waiting[0]["reason"])
        self.assertTrue(self.notices)
        self.assertEqual(self.reboots, [])

    async def test_reboot_runs_when_policy_allows(self) -> None:
        pol = await self.store.get_policy()
        pol.confirm_nothing = True
        pol.confirm_kernel_docker = False
        await self.store.save_policy(pol)
        job = _BakJob("p1", "lxc:a")
        job.kind = "apply"
        job.status = "success"
        job.result = {"reboot_required": True, "packages": ["linux-image-6.8"]}
        row = await self._win(
            kind=KIND_PATCH,
            target_id="lxc:a",
            hm="20:00",
            bucket="regular",
            status=STATUS_DONE,
            packages=["linux-image-6.8"],
        )
        await self.engine._maybe_reboot_after_apply(row, job)
        self.assertEqual(self.reboots, ["lxc:a"])
        log = await self.store.list_activity()
        self.assertTrue(any(r.get("action") == ACTION_REBOOT for r in log))
        self.assertIn(REASON_REBOOT_DONE.split()[0], log[0]["detail"])

    async def test_capacity_warns_before_skipping_unfit(self) -> None:
        self.dest = {
            "hetzner": {
                "quota_known": True,
                "used_bytes": 90,
                "free_bytes": 10,
                "total_bytes": 100,
                "used_pct": 90.0,
                "label": "Hetzner",
            }
        }
        store = _RunStore()
        self.engine._get_backup_store = lambda: store
        await self._win(kind=KIND_BACKUP, target_id="lxc:big", hm="20:00", stack="big")
        await self.engine._warn_capacity()
        waiting = await self.store.list_windows(statuses=[STATUS_WAITING])
        self.assertTrue(any(w.get("bucket") == "capacity" for w in waiting))
        self.assertTrue(any(r.get("action") == ACTION_WARN for r in await self.store.list_activity()))

    async def test_copilot_data_without_stack_does_not_inject_host(self) -> None:
        await self.engine._prompt_copilot_data()
        prompts = await self.store.list_scope_prompts(status="waiting")
        self.assertFalse(any(p.get("target_id") == COPILOT_DATA_ID for p in prompts))
        known = await self.store.list_known_hosts()
        self.assertFalse(any(h.get("target_id") == COPILOT_DATA_ID for h in known))
        matrix = await self.engine.scope_matrix()
        self.assertFalse(any(h.get("id") == COPILOT_DATA_ID for h in matrix["hosts"]))

    async def test_vanished_host_drops_missing_backup_and_kurzlage(self) -> None:
        await self.store.seed_known_hosts(
            [{"id": "lxc:pve:1", "name": "mail", "kind": "lxc"}]
        )
        await self.store.upsert_known_host(
            target_id="lxc:old", target_name="alt", kind="lxc", gone=True
        )
        await self.store.insert_scope_prompt(
            target_id="lxc:old",
            target_name="alt",
            kind="no_backup",
            reason="alt hat keinen Backup-Plan. So gewollt?",
        )
        await self.store.insert_activity(
            action=ACTION_WARN,
            result="wait",
            kind=KIND_BACKUP,
            target_id="lxc:old",
            target_name="alt",
            detail="alt hat keinen Backup-Plan. So gewollt?",
        )
        await self.engine._prompt_missing_backups(
            [{"id": "lxc:pve:1", "name": "mail", "kind": "lxc"}]
        )
        waiting = await self.store.list_scope_prompts(status="waiting")
        self.assertFalse(any(p.get("target_id") == "lxc:old" for p in waiting))
        self.assertTrue(any(p.get("target_id") == "lxc:pve:1" for p in waiting))
        old = next(h for h in await self.store.list_known_hosts() if h["target_id"] == "lxc:old")
        self.assertIsNone(old.get("skip_backup"))
        brief = await self.engine.refresh_evening_brief(force=True)
        self.assertNotIn("alt hat keinen Backup-Plan", brief["text"])
        self.assertNotIn("Copilot /data", brief["text"])

    async def test_leftover_copilot_host_is_removed_not_nagged(self) -> None:
        await self.store.upsert_known_host(
            target_id=COPILOT_DATA_ID,
            target_name="Copilot /data",
            kind="copilot",
            gone=True,
        )
        await self.store.insert_scope_prompt(
            target_id=COPILOT_DATA_ID,
            target_name="Copilot /data",
            kind="no_backup",
            reason="Copilot /data hat keinen Backup-Plan. So gewollt?",
        )
        await self.store.insert_activity(
            action=ACTION_WARN,
            result="wait",
            kind=KIND_BACKUP,
            target_id=COPILOT_DATA_ID,
            target_name="Copilot /data",
            detail=(
                "Copilot /data hat keinen Backup-Plan. So gewollt? "
                "Kein bestehender Copilot-Backup-Job — nichts erfunden durch Agent."
            ),
        )
        await self.engine.reconcile_hosts()
        known = await self.store.list_known_hosts()
        self.assertFalse(any(h.get("target_id") == COPILOT_DATA_ID for h in known))
        matrix = await self.engine.scope_matrix()
        self.assertFalse(any(h.get("id") == COPILOT_DATA_ID for h in matrix["hosts"]))
        waiting = await self.store.list_scope_prompts(status="waiting")
        self.assertFalse(any(p.get("target_id") == COPILOT_DATA_ID for p in waiting))
        brief = await self.engine.refresh_evening_brief(force=True)
        self.assertNotIn("Copilot /data", brief["text"])

    async def test_copilot_stack_prompts_without_fake_host(self) -> None:
        async def _stacks(_snap: object) -> list[dict]:
            return [
                {
                    "parent_id": "lxc:pve:1",
                    "stack": "homelab-copilot",
                    "guest_name": "copilot",
                }
            ]

        self.engine._list_backup_stacks = _stacks
        await self.engine._prompt_copilot_data()
        prompts = await self.store.list_scope_prompts(status="waiting")
        self.assertTrue(any(p.get("target_id") == COPILOT_DATA_ID for p in prompts))
        self.assertIn("Copilot /data", prompts[0]["reason"])
        known = await self.store.list_known_hosts()
        self.assertFalse(any(h.get("target_id") == COPILOT_DATA_ID for h in known))
        matrix = await self.engine.scope_matrix()
        self.assertFalse(any(h.get("id") == COPILOT_DATA_ID for h in matrix["hosts"]))

    async def test_evening_brief_from_log(self) -> None:
        await self.store.insert_activity(
            action=ACTION_SKIPPED,
            result="skip",
            target_name="mail",
            detail=REASON_OFFLINE_TODAY.format(name="mail"),
        )
        await self.store.insert_activity(
            action="apply",
            result="ok",
            kind=KIND_BACKUP,
            target_name="paperless",
            detail="Backup erfolgreich",
        )
        brief = await self.engine.refresh_evening_brief(force=True)
        self.assertIn("offline", brief["text"].lower())
        self.assertIn("Backup", brief["text"])
        self.assertIn("fällt auf", brief["text"])

    async def test_propose_creates_patch_window_for_ticked_pending_host(self) -> None:
        policy = await self.store.get_policy()
        policy.patch_scope_ids = ["lxc:pve:1"]
        policy.image_scope_ids = []
        await self.store.save_policy(policy)

        async def _pending(*_a: object, **_k: object) -> list:
            return [
                HostPending(
                    target_id="lxc:pve:1",
                    target_name="mail",
                    packages=[{"name": "openssl", "priority": "security"}],
                )
            ]

        self.engine._hosts_from_store = _pending
        result = await self.engine.propose()
        created = result.get("created") or []
        self.assertTrue(
            any(
                str(w.get("kind")) == KIND_PATCH and str(w.get("bucket")) == "security"
                for w in created
            )
        )

    async def test_collect_needs_ignores_discovered_stacks_without_schedule(self) -> None:
        async def _stacks(_snap: object) -> list[dict]:
            return [
                {
                    "parent_id": "lxc:pve:1",
                    "stack": "portaineragent",
                    "guest_name": "mail",
                    "engine": "tar",
                }
            ]

        self.engine._list_backup_stacks = _stacks
        self.engine._get_backup_store = lambda: _SchedStore([])
        policy = await self.engine.policy()
        needs = await self.engine._collect_needs(policy)
        self.assertFalse(any(n.kind == KIND_BACKUP for n in needs))

    async def test_propose_does_not_invent_backup_for_discovered_stacks(self) -> None:
        async def _stacks(_snap: object) -> list[dict]:
            return [
                {
                    "parent_id": "lxc:pve:1",
                    "stack": "portaineragent",
                    "guest_name": "mail",
                    "engine": "tar",
                }
            ]

        store = _SchedStore([])
        self.engine._list_backup_stacks = _stacks
        self.engine._get_backup_store = lambda: store
        result = await self.engine.propose()
        created = result.get("created") or []
        self.assertFalse(
            any(str(w.get("stack") or "") == "portaineragent" for w in created)
        )
        windows = await self.store.list_windows()
        self.assertFalse(
            any(str(w.get("stack") or "") == "portaineragent" for w in windows)
        )
        self.assertEqual(store.upserts, [])

    async def test_plan_backup_for_host_does_not_invent_discovered_stacks(self) -> None:
        async def _stacks(_snap: object) -> list[dict]:
            return [
                {
                    "parent_id": "lxc:pve:1",
                    "stack": "portaineragent",
                    "guest_name": "mail",
                }
            ]

        self.engine._list_backup_stacks = _stacks
        self.engine._get_backup_store = lambda: _SchedStore([])
        created = await self.engine._plan_backup_for_host("lxc:pve:1", "mail")
        self.assertEqual(created, [])
        windows = await self.store.list_windows()
        self.assertEqual(
            [w for w in windows if str(w.get("kind") or "") == KIND_BACKUP], []
        )

    async def test_ingest_keeps_restic_engine(self) -> None:
        store = _SchedStore(
            [
                {
                    "id": 3,
                    "parent_id": "lxc:pve:1",
                    "stack": "paperless",
                    "guest_name": "mail",
                    "enabled": True,
                    "cron_expr": "0 3 * * *",
                    "engine": "restic",
                    "restic_full_every_days": 3,
                    "restic_keep_last": 21,
                    "restic_keep_weekly": 4,
                }
            ]
        )
        self.engine._get_backup_store = lambda: store
        rows = await self.engine.ingest(now=_now())
        bak = [r for r in rows if r.get("kind") == KIND_BACKUP]
        self.assertEqual(len(bak), 1)
        self.assertEqual(bak[0]["engine"], "restic")
        self.assertEqual(bak[0]["stack"], "paperless")
        self.assertEqual(bak[0]["source"], "ingested")
        self.assertEqual(bak[0]["schedule_id"], 3)

    async def test_ingest_creates_next_day_after_done(self) -> None:
        store = _SchedStore(
            [
                {
                    "id": 9,
                    "parent_id": "lxc:pve:1",
                    "stack": "weatherapp",
                    "guest_name": "mail",
                    "enabled": True,
                    "cron_expr": "0 20 * * *",
                }
            ]
        )
        self.engine._get_backup_store = lambda: store
        today = datetime(2026, 9, 6, 20, 0, tzinfo=BERLIN)
        await self.store.insert_window(
            {
                "kind": KIND_BACKUP,
                "target_id": "lxc:pve:1",
                "target_name": "mail",
                "stack": "weatherapp",
                "start_iso": iso_utc(today),
                "start_hm": "20:00",
                "status": STATUS_DONE,
                "source": SOURCE_INGESTED,
                "schedule_id": 9,
                "reason": "Backup erfolgreich",
            }
        )
        nxt = datetime(2026, 9, 7, 0, 10, tzinfo=BERLIN)
        rows = await self.engine.ingest(now=nxt)
        bak = [r for r in rows if r.get("kind") == KIND_BACKUP]
        self.assertTrue(any(str(r.get("status")) == "accepted" for r in bak))
        accepted = [r for r in bak if r.get("status") == "accepted"][0]
        self.assertEqual(accepted["start_hm"], "20:00")
        start = accepted.get("start_iso") or ""
        self.assertTrue("2026-09-07" in start or accepted["start_hm"] == "20:00")
        windows = await self.store.list_windows()
        self.assertGreaterEqual(len([w for w in windows if w.get("schedule_id") == 9]), 2)

    async def test_ingest_does_not_duplicate_same_day_done(self) -> None:
        store = _SchedStore(
            [
                {
                    "id": 10,
                    "parent_id": "lxc:pve:1",
                    "stack": "weatherapp",
                    "guest_name": "mail",
                    "enabled": True,
                    "cron_expr": "0 20 * * *",
                }
            ]
        )
        self.engine._get_backup_store = lambda: store
        start = datetime(2026, 9, 6, 20, 0, tzinfo=BERLIN)
        await self.store.insert_window(
            {
                "kind": KIND_BACKUP,
                "target_id": "lxc:pve:1",
                "target_name": "mail",
                "stack": "weatherapp",
                "start_iso": iso_utc(start),
                "start_hm": "20:00",
                "status": STATUS_DONE,
                "source": SOURCE_INGESTED,
                "schedule_id": 10,
            }
        )
        rows = await self.engine.ingest(now=datetime(2026, 9, 6, 18, 30, tzinfo=BERLIN))
        bak = [r for r in rows if r.get("kind") == KIND_BACKUP]
        self.assertEqual(len(bak), 1)
        self.assertEqual(bak[0]["status"], STATUS_DONE)
        self.assertEqual(len([w for w in await self.store.list_windows() if w.get("schedule_id") == 10]), 1)

    async def test_plan_backup_for_host_uses_existing_restic_schedule(self) -> None:
        store = _SchedStore(
            [
                {
                    "id": 5,
                    "parent_id": "lxc:pve:1",
                    "stack": "paperless",
                    "guest_name": "mail",
                    "enabled": True,
                    "cron_expr": "0 20 * * *",
                    "engine": "restic",
                }
            ]
        )
        self.engine._get_backup_store = lambda: store
        created = await self.engine._plan_backup_for_host("lxc:pve:1", "mail")
        self.assertTrue(created)
        self.assertEqual(created[0]["engine"], "restic")
        self.assertEqual(created[0]["stack"], "paperless")
        self.assertEqual(created[0]["schedule_id"], 5)
        self.assertEqual(store.upserts, [])

    async def test_ensure_backup_schedule_does_not_create(self) -> None:
        store = _SchedStore([])
        self.engine._get_backup_store = lambda: store
        win = await self._win(
            kind=KIND_BACKUP, target_id="lxc:pve:1", hm="20:00", stack="portaineragent"
        )
        await self.engine._ensure_backup_schedule(win, _now())
        self.assertEqual(store.upserts, [])
        fresh = await self.store.get_window(int(win["id"]))
        assert fresh is not None
        self.assertFalse(fresh.get("schedule_id"))

    async def test_ensure_backup_schedule_prefers_existing_restic(self) -> None:
        store = _SchedStore(
            [
                {
                    "id": 11,
                    "parent_id": "lxc:pve:1",
                    "stack": "paperless",
                    "engine": "tar",
                },
                {
                    "id": 12,
                    "parent_id": "lxc:pve:1",
                    "stack": "paperless",
                    "engine": "restic",
                    "restic_keep_last": 21,
                },
            ]
        )
        self.engine._get_backup_store = lambda: store
        win = await self._win(
            kind=KIND_BACKUP, target_id="lxc:pve:1", hm="20:00", stack="paperless"
        )
        await self.engine._ensure_backup_schedule(win, _now())
        self.assertEqual(store.upserts, [])
        fresh = await self.store.get_window(int(win["id"]))
        assert fresh is not None
        self.assertEqual(int(fresh["schedule_id"]), 12)

    async def test_no_backup_yes_does_not_invent_schedules(self) -> None:
        await self.store.seed_known_hosts(
            [{"id": "lxc:pve:1", "name": "mail", "kind": "lxc"}]
        )
        await self.engine._prompt_missing_backups(
            [{"id": "lxc:pve:1", "name": "mail", "kind": "lxc"}]
        )
        prompts = await self.store.list_scope_prompts(status="waiting")
        self.assertEqual(len(prompts), 1)

        async def _stacks(_snap: object) -> list[dict]:
            return [
                {
                    "parent_id": "lxc:pve:1",
                    "stack": "portaineragent",
                    "guest_name": "mail",
                }
            ]

        store = _SchedStore([])
        self.engine._list_backup_stacks = _stacks
        self.engine._get_backup_store = lambda: store
        await self.engine.answer_host_prompt(int(prompts[0]["id"]), backup=True)
        self.assertEqual(store.upserts, [])
        windows = await self.store.list_windows()
        self.assertFalse(
            any(str(w.get("stack") or "") == "portaineragent" for w in windows)
        )

    async def test_shift_restic_window_keeps_engine_and_retention(self) -> None:
        store = _SchedStore(
            [
                {
                    "id": 9,
                    "parent_id": "lxc:b",
                    "stack": "paperless",
                    "guest_name": "mail",
                    "enabled": True,
                    "preset": "daily",
                    "cron_expr": "20 21 * * *",
                    "note": "paperless restic",
                    "engine": "restic",
                    "restic_full_every_days": 3,
                    "restic_keep_last": 21,
                    "restic_keep_weekly": 4,
                }
            ]
        )
        self.engine._get_backup_store = lambda: store
        await self._win(kind=KIND_BACKUP, target_id="lxc:a", hm="20:00", stack="one")
        later = await self._win(
            kind=KIND_BACKUP,
            target_id="lxc:b",
            hm="21:20",
            stack="paperless",
            schedule_id=9,
            engine="restic",
        )
        result = await self.engine.start_accepted_now()
        self.assertEqual(self.started_backup, ["lxc:a"])
        moved = await self.store.get_window(int(later["id"]))
        assert moved is not None
        self.assertEqual(moved["reason"], REASON_BACKUP_CHAIN)
        self.assertNotEqual(moved["start_hm"], "21:20")
        self.assertEqual(moved.get("engine") or "restic", "restic")
        self.assertEqual(store.upserts, [])
        sched = await store.get_schedule(9)
        assert sched is not None
        self.assertEqual(sched["cron_expr"], "20 21 * * *")
        self.assertEqual(sched["engine"], "restic")
        self.assertTrue(result["shifted"])

    async def test_rewrite_schedule_time_passes_through_restic_retention(self) -> None:
        store = _SchedStore(
            [
                {
                    "id": 4,
                    "parent_id": "lxc:pve:1",
                    "stack": "paperless",
                    "preset": "daily",
                    "enabled": True,
                    "note": "keep-me",
                    "engine": "restic",
                    "restic_full_every_days": 2,
                    "restic_keep_last": 30,
                    "restic_keep_weekly": 0,
                }
            ]
        )
        self.engine._get_backup_store = lambda: store
        await self.engine._rewrite_schedule_time(4, "22:40")
        self.assertEqual(len(store.upserts), 1)
        last = store.upserts[0]
        self.assertEqual(last["engine"], "restic")
        self.assertEqual(last["restic_full_every_days"], 2)
        self.assertEqual(last["restic_keep_last"], 30)
        self.assertEqual(last["restic_keep_weekly"], 0)
        self.assertEqual(last["note"], "keep-me")
        self.assertIn("22", last["cron_expr"])
        self.assertIn("40", last["cron_expr"])


class _SchedStore:
    def __init__(self, schedules: list[dict] | None = None) -> None:
        self.schedules = [dict(s) for s in (schedules or [])]
        self.upserts: list[dict] = []

    async def list_schedules(self) -> list[dict]:
        return [dict(s) for s in self.schedules]

    async def get_schedule(self, schedule_id: int) -> dict | None:
        for row in self.schedules:
            if int(row.get("id") or 0) == int(schedule_id):
                return dict(row)
        return None

    async def find_schedules_for_stack(self, parent_id: str, stack: str) -> list[dict]:
        return [
            dict(s)
            for s in self.schedules
            if str(s.get("parent_id") or "") == parent_id
            and str(s.get("stack") or "") == stack
        ]

    async def upsert_schedule(self, **kwargs: object) -> int:
        self.upserts.append(dict(kwargs))
        sid = kwargs.get("schedule_id")
        if sid is not None:
            for row in self.schedules:
                if int(row.get("id") or 0) == int(sid):  # type: ignore[arg-type]
                    row.update({k: v for k, v in kwargs.items() if k != "schedule_id"})
                    return int(sid)  # type: ignore[arg-type]
        new_id = max((int(s.get("id") or 0) for s in self.schedules), default=0) + 1
        row = {"id": new_id, **kwargs}
        self.schedules.append(row)
        return new_id

    async def list_runs_for_stack(self, parent_id: str, stack: str, limit: int = 8):
        return []


class ScheduleMethodFieldTests(unittest.TestCase):
    def test_restic_zero_keep_weekly_is_kept(self) -> None:
        fields = _schedule_method_fields(
            {
                "engine": "restic",
                "restic_full_every_days": 3,
                "restic_keep_last": 21,
                "restic_keep_weekly": 0,
            }
        )
        self.assertEqual(fields["engine"], "restic")
        self.assertEqual(fields["restic_full_every_days"], 3)
        self.assertEqual(fields["restic_keep_last"], 21)
        self.assertEqual(fields["restic_keep_weekly"], 0)

    def test_missing_engine_stays_tar_not_invented_restic(self) -> None:
        fields = _schedule_method_fields({"restic_keep_last": 14})
        self.assertEqual(fields["engine"], "tar")


class _RunStore:
    async def list_runs_for_stack(self, parent_id: str, stack: str, limit: int = 8):
        return [{"status": "success", "size_bytes": 50}]

    async def list_schedules(self):
        return []


class EveningBriefFilterTests(unittest.TestCase):
    def test_missing_backup_warn_for_vanished_is_dropped(self) -> None:
        from ops_agent.activity import ACTION_APPLY, ACTION_WARN, build_evening_brief
        from app.core.locale import iso_utc

        now = _now()
        iso = iso_utc(now)
        text = build_evening_brief(
            [
                {
                    "action": ACTION_APPLY,
                    "kind": KIND_BACKUP,
                    "result": "ok",
                    "target_name": "mail",
                    "created_at_iso": iso,
                },
                {
                    "action": ACTION_WARN,
                    "kind": KIND_BACKUP,
                    "result": "wait",
                    "target_id": "lxc:old",
                    "target_name": "alt",
                    "detail": "alt hat keinen Backup-Plan. So gewollt?",
                    "created_at_iso": iso,
                },
            ],
            now=now,
            gone_ids={"lxc:old"},
            live_ids={"lxc:pve:1"},
        )
        self.assertIn("Backup", text)
        self.assertNotIn("alt hat keinen Backup-Plan", text)
        self.assertNotIn("Warnung", text)


class CapacityHelperTests(unittest.TestCase):
    def test_median_history(self) -> None:
        self.assertEqual(
            estimate_bytes_from_runs(
                [
                    {"status": "success", "size_bytes": 10},
                    {"status": "success", "size_bytes": 20},
                    {"status": "failed", "size_bytes": 999},
                    {"status": "success", "size_bytes": 30},
                ]
            ),
            20,
        )


if __name__ == "__main__":
    unittest.main()
