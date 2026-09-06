"""Missing-backup prompt, backup chain, capacity/offline/hung/reboot — ohne Proxmox."""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.core.locale import BERLIN, iso_utc
from ops_agent.engine import OpsEngine
from ops_agent.activity import ACTION_REBOOT, ACTION_SKIPPED, ACTION_WARN
from ops_agent.capacity import estimate_bytes_from_runs
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
            _Ent(id="lxc:pve:1", kind="lxc", name="mail", status=status, node="pve", meta=meta),
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
            self.bak_jobs.append(_BakJob(jid, tid, created_at=_now().timestamp()))
            return jid

        async def _start_patch(_row: dict) -> tuple[bool, str, str | None]:
            return True, "", "p-1"

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

    async def test_copilot_data_prompt_without_job_type(self) -> None:
        await self.engine._prompt_copilot_data()
        prompts = await self.store.list_scope_prompts(status="waiting")
        self.assertTrue(any(p.get("target_id") == "copilot:data" for p in prompts))
        self.assertIn("Copilot /data", prompts[0]["reason"])

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


class _RunStore:
    async def list_runs_for_stack(self, parent_id: str, stack: str, limit: int = 8):
        return [{"status": "success", "size_bytes": 50}]

    async def list_schedules(self):
        return []


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
