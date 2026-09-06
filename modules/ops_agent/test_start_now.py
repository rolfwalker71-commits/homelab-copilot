"""Start accepted windows now + replan shift — ohne LLM, ohne Proxmox."""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from app.core.locale import BERLIN, iso_utc
from ops_agent.actor import VIA_AGENT
from ops_agent.engine import OpsEngine
from ops_agent.planner import (
    KIND_BACKUP,
    KIND_DRILL,
    KIND_PATCH,
    REASON_BACKUP_OVERRUN,
    SOURCE_AGENT,
    SOURCE_DRILL,
    SOURCE_INGESTED,
    STATUS_ACCEPTED,
    STATUS_RUNNING,
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
        project: str = "paperless",
    ) -> None:
        self.id = job_id
        self.parent_id = parent_id
        self.kind = kind
        self.project = project
        self.created_at = created_at if created_at is not None else time.time()


class _BakStore:
    def __init__(self) -> None:
        self.fired: list[tuple[int, str]] = []

    async def mark_schedule_fired(self, schedule_id: int, *, minute_key: str) -> None:
        self.fired.append((int(schedule_id), minute_key))


def _now() -> datetime:
    return datetime(2026, 9, 6, 18, 0, tzinfo=BERLIN)


class StartAcceptedNowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = OpsStore(Path(self.tmp.name) / "ops.db")
        await self.store.connect()
        self.bak_jobs: list[_BakJob] = []
        self.patch_jobs: list[object] = []
        self.started_backup: list[str] = []
        self.started_patch: list[str] = []
        self.bak_store = _BakStore()
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

        async def _start_patch(row: dict) -> tuple[bool, str, str | None]:
            tid = str(row.get("target_id") or "")
            self.started_patch.append(tid)
            return True, "", f"p-{len(self.started_patch)}"

        return OpsEngine(
            self.store,
            get_snapshot=lambda: None,
            get_backup_store=lambda: self.bak_store,
            list_backup_stacks=_empty,
            hosts_from_store=_empty,
            start_backup=_start_backup,
            start_patch=_start_patch,
            list_backup_jobs=lambda: list(self.bak_jobs),
            list_patch_jobs=lambda: list(self.patch_jobs),
        )

    async def _win(
        self,
        *,
        kind: str,
        target_id: str,
        hm: str,
        status: str = STATUS_ACCEPTED,
        bucket: str = "",
        source: str = SOURCE_AGENT,
        needs_confirm: bool = False,
        schedule_id: int | None = None,
        stack: str = "",
    ) -> dict:
        hour, minute = (int(x) for x in hm.split(":"))
        start = _now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        if hour < 18:
            start = start.replace(day=7)
        wid = await self.store.insert_window(
            {
                "kind": kind,
                "target_id": target_id,
                "target_name": target_id,
                "stack": stack or ("paperless" if kind == KIND_BACKUP else ""),
                "bucket": bucket or kind,
                "start_iso": iso_utc(start),
                "start_hm": hm,
                "duration_min": 20 if kind == KIND_PATCH else 10,
                "status": status,
                "source": source,
                "schedule_id": schedule_id,
                "needs_confirm": needs_confirm,
                "reason": "Testfenster",
            }
        )
        row = await self.store.get_window(wid)
        assert row is not None
        return row

    async def test_starts_backups_then_one_patch_skips_waiting(self) -> None:
        policy = await self.store.get_policy()
        policy.patch_scope_ids = ["lxc:a", "lxc:b", "lxc:c", "lxc:wait"]
        policy.image_scope_ids = list(policy.patch_scope_ids)
        await self.store.save_policy(policy)

        bak_early = await self._win(
            kind=KIND_BACKUP, target_id="lxc:a", hm="20:00", schedule_id=7,
            source=SOURCE_INGESTED,
        )
        bak_late = await self._win(kind=KIND_BACKUP, target_id="lxc:b", hm="20:10")
        patch_a = await self._win(
            kind=KIND_PATCH, target_id="lxc:a", hm="20:20", bucket="security"
        )
        patch_c = await self._win(
            kind=KIND_PATCH, target_id="lxc:c", hm="20:40", bucket="security"
        )
        patch_late = await self._win(
            kind=KIND_PATCH, target_id="lxc:b", hm="21:00", bucket="security"
        )
        waiting = await self._win(
            kind=KIND_PATCH,
            target_id="lxc:wait",
            hm="21:10",
            bucket="regular",
            status=STATUS_WAITING,
            needs_confirm=True,
        )
        await self._win(
            kind=KIND_DRILL, target_id="*", hm="05:00", source=SOURCE_DRILL
        )

        result = await self.engine.start_accepted_now()
        self.assertEqual(self.started_backup, ["lxc:a"])
        self.assertEqual(self.started_patch, ["lxc:c"])
        self.assertEqual(len(result["started"]), 2)
        started_ids = {int(w["id"]) for w in result["started"]}
        self.assertIn(int(bak_early["id"]), started_ids)
        self.assertNotIn(int(bak_late["id"]), started_ids)
        self.assertIn(int(patch_c["id"]), started_ids)
        later_b = await self.store.get_window(int(bak_late["id"]))
        assert later_b is not None
        self.assertEqual(later_b["status"], STATUS_ACCEPTED)
        self.assertIn("Anschluss", later_b["reason"])
        self.assertNotIn(int(patch_a["id"]), started_ids)
        self.assertNotIn(int(patch_late["id"]), started_ids)
        self.assertNotIn(int(waiting["id"]), started_ids)
        fresh_a = await self.store.get_window(int(patch_a["id"]))
        assert fresh_a is not None
        self.assertEqual(fresh_a["status"], STATUS_ACCEPTED)
        fresh_w = await self.store.get_window(int(waiting["id"]))
        assert fresh_w is not None
        self.assertEqual(fresh_w["status"], STATUS_WAITING)
        self.assertEqual(self.bak_store.fired[0][0], 7)

    async def test_skips_patch_when_backup_already_running(self) -> None:
        policy = await self.store.get_policy()
        policy.patch_scope_ids = ["lxc:busy"]
        await self.store.save_policy(policy)
        self.bak_jobs.append(_BakJob("live", "lxc:busy", created_at=_now().timestamp()))
        await self._win(
            kind=KIND_PATCH, target_id="lxc:busy", hm="20:00", bucket="security"
        )
        result = await self.engine.start_accepted_now()
        self.assertEqual(self.started_patch, [])
        self.assertEqual(result["started"], [])
        self.assertTrue(result["skipped"])
        self.assertIn("läuft bereits", result["skipped"][0]["reason"])

    async def test_replans_later_window_labeled_durch_agent(self) -> None:
        policy = await self.store.get_policy()
        policy.patch_scope_ids = ["lxc:1"]
        await self.store.save_policy(policy)
        bak = await self._win(kind=KIND_BACKUP, target_id="lxc:1", hm="18:00")
        later = await self._win(
            kind=KIND_PATCH, target_id="lxc:1", hm="18:00", bucket="security"
        )
        result = await self.engine.start_accepted_now()
        self.assertEqual(self.started_backup, ["lxc:1"])
        self.assertEqual(self.started_patch, [])
        self.assertTrue(result["shifted"])
        shift = result["shifted"][0]
        self.assertEqual(int(shift["window_id"]), int(later["id"]))
        self.assertEqual(shift["old_start_hm"], "18:00")
        self.assertNotEqual(shift["new_start_hm"], "18:00")
        self.assertIn(REASON_BACKUP_OVERRUN.split("—")[0].strip(), shift["reason"])
        self.assertIn(VIA_AGENT, shift["reason"])
        moved = await self.store.get_window(int(later["id"]))
        assert moved is not None
        self.assertEqual(moved["status"], STATUS_ACCEPTED)
        self.assertIn(VIA_AGENT, moved["reason"])
        running = await self.store.get_window(int(bak["id"]))
        assert running is not None
        self.assertEqual(running["status"], STATUS_RUNNING)

    async def test_non_runtime_error_skips_and_starts_next(self) -> None:
        class Boom(Exception):
            pass

        original = self.engine._start_backup

        async def _boom(row: dict) -> str | None:
            if row.get("target_id") == "lxc:boom":
                raise Boom("Backup-Store nicht bereit.")
            return await original(row)

        self.engine._start_backup = _boom
        await self._win(kind=KIND_BACKUP, target_id="lxc:boom", hm="20:00", stack="bad")
        ok = await self._win(kind=KIND_BACKUP, target_id="lxc:ok", hm="20:10", stack="good")
        result = await self.engine.start_accepted_now()
        self.assertEqual(self.started_backup, ["lxc:ok"])
        self.assertEqual(len(result["started"]), 1)
        self.assertEqual(int(result["started"][0]["id"]), int(ok["id"]))
        self.assertTrue(any("Backup-Store nicht bereit" in s["reason"] for s in result["skipped"]))
        self.assertIn("Läuft", result["message"])
        self.assertIn("good", result["message"])

    async def test_empty_start_message_explains_why(self) -> None:
        result = await self.engine.start_accepted_now()
        self.assertEqual(result["started"], [])
        self.assertIn("Nichts gestartet", result["message"])
        self.assertIn("Als Nächstes", result["message"])

    async def test_hard_stop_and_waiting_never_started(self) -> None:
        await self._win(
            kind="distupgrade",
            target_id="lxc:x",
            hm="20:00",
            bucket="distupgrade",
        )
        result = await self.engine.start_accepted_now()
        self.assertEqual(self.started_backup, [])
        self.assertEqual(self.started_patch, [])
        self.assertEqual(result["started"], [])
        self.assertIn("Nichts gestartet", result["message"])
        self.assertTrue(
            "nicht selbst starten" in result["message"]
            or "harten Stopp" in result["message"]
            or "Harter Stopp" in result["message"]
        )


if __name__ == "__main__":
    unittest.main()
