"""Autonomous pre-apply rollback + Agent-Zuschreibung — ohne Proxmox."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.core.locale import iso_utc, now_berlin
from ops_agent.actor import VIA_AGENT, actor_fields, agent_phrase, by_agent
from ops_agent.engine import OpsEngine
from ops_agent.planner import KIND_PATCH, SOURCE_AGENT, STATUS_ACCEPTED, STATUS_RUNNING
from ops_agent.rollback import (
    REASON_APPLY_FAILED,
    REASON_RATE_LIMIT,
    REASON_TIMEOUT,
    REASON_UNHEALTHY,
    SKIP_RATE_LIMIT,
    classify_fail_reason,
    plan_rollback,
    snap_name_from_logs,
    window_reason_after_rollback,
)
from ops_agent.store import OpsStore
from patcher.jobs import JOBS


def _engine(
    store: OpsStore,
    *,
    rollback: AsyncMock | None = None,
) -> OpsEngine:
    async def _empty(*_a: object, **_k: object) -> list:
        return []

    async def _start_patch(_row: dict) -> tuple[bool, str, str | None]:
        return False, "unused", None

    async def _start_backup(_row: dict) -> str | None:
        return None

    return OpsEngine(
        store,
        get_snapshot=lambda: None,
        get_backup_store=lambda: None,
        list_backup_stacks=_empty,
        hosts_from_store=_empty,
        start_backup=_start_backup,
        start_patch=_start_patch,
        list_backup_jobs=lambda: [],
        list_patch_jobs=lambda: [],
        rollback_guest_snap=rollback,
    )


class ActorLabelTests(unittest.TestCase):
    def test_by_agent_once(self) -> None:
        self.assertEqual(by_agent("Patches eingespielt"), "Patches eingespielt durch Agent")
        self.assertEqual(
            by_agent("Patches eingespielt durch Agent"),
            "Patches eingespielt durch Agent",
        )
        self.assertEqual(by_agent("Fertig.", via_agent=False), "Fertig.")
        self.assertIn(VIA_AGENT, agent_phrase("images_applied"))
        self.assertIn("hlops-a", agent_phrase("rolled_back", snap="hlops-a"))
        self.assertTrue(actor_fields(via_agent=True)["via_agent"])
        self.assertEqual(actor_fields(via_agent=False)["actor_label"], "")


class PlanRollbackTests(unittest.TestCase):
    def test_classify(self) -> None:
        self.assertEqual(classify_fail_reason("SSH-Befehl-Timeout zu 1.2.3.4"), REASON_TIMEOUT)
        self.assertEqual(classify_fail_reason("Gast unhealthy after apply"), REASON_UNHEALTHY)
        self.assertEqual(classify_fail_reason("apt-get exit 1"), REASON_APPLY_FAILED)
        self.assertEqual(
            classify_fail_reason(
                "Error toomanyrequests: You have reached your unauthenticated pull rate limit."
            ),
            REASON_RATE_LIMIT,
        )

    def test_rate_limit_never_rolls_back(self) -> None:
        plan = plan_rollback(
            job_kind="image-apply",
            target_id="lxc:1",
            result={"snapshot": {"skipped": False, "name": "hlops-mail"}},
            error="docker compose pull fehlgeschlagen: Error toomanyrequests",
        )
        self.assertEqual(plan.action, "skip")
        self.assertEqual(plan.reason_code, REASON_RATE_LIMIT)
        self.assertEqual(plan.skip_reason, SKIP_RATE_LIMIT)
        self.assertIn("Kein Rollback", plan.skip_reason)

    def test_snap_from_result_or_log(self) -> None:
        plan = plan_rollback(
            job_kind="apply",
            target_id="lxc:1",
            result={"snapshot": {"skipped": False, "name": "hlops-job"}},
            error="fail",
        )
        self.assertEqual(plan.action, "rollback")
        self.assertEqual(plan.snap_name, "hlops-job")
        self.assertEqual(
            snap_name_from_logs(["Proxmox-Snapshot „hlops-log“ angelegt durch Agent."]),
            "hlops-log",
        )

    def test_no_snap_skips(self) -> None:
        plan = plan_rollback(
            job_kind="apply",
            target_id="lxc:1",
            result={"snapshot": {"skipped": True, "reason": "manual"}},
            error="fail",
        )
        self.assertEqual(plan.action, "skip")
        self.assertIn("Snapshot", plan.skip_reason)

    def test_distupgrade_never(self) -> None:
        plan = plan_rollback(
            job_kind="release-upgrade",
            target_id="lxc:1",
            result={"snapshot": {"skipped": False, "name": "hlops-x"}},
            error="hop failed",
        )
        self.assertEqual(plan.action, "skip")
        self.assertIn("Auftragstyp", plan.skip_reason)

    def test_already_skips(self) -> None:
        plan = plan_rollback(
            job_kind="apply",
            target_id="lxc:1",
            result={"snapshot": {"name": "hlops-x"}},
            error="fail",
            already=True,
        )
        self.assertEqual(plan.action, "skip")


class RollbackEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = OpsStore(Path(self.tmp.name) / "ops.db")
        await self.store.connect()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.tmp.cleanup()

    async def _window(self, *, job_id: str, bucket: str = "security") -> dict:
        now = now_berlin()
        wid = await self.store.insert_window(
            {
                "kind": KIND_PATCH,
                "target_id": "lxc:pve:1",
                "target_name": "mail",
                "bucket": bucket,
                "start_iso": iso_utc(now),
                "start_hm": "20:00",
                "status": STATUS_RUNNING,
                "source": SOURCE_AGENT,
                "job_id": job_id,
                "reason": agent_phrase("window_planned"),
            }
        )
        row = await self.store.get_window(wid)
        assert row is not None
        return row

    async def test_fail_with_snap_rolls_back_and_audits(self) -> None:
        job = JOBS.create(kind="apply", target_id="lxc:pve:1", via_agent=True)
        JOBS.finish(
            job.id,
            status="failed",
            error="apt-get exit 1",
            result={"snapshot": {"skipped": False, "name": "hlops-a"}},
        )
        calls: list[tuple[str, str]] = []

        async def rb(tid: str, name: str) -> dict:
            calls.append((tid, name))
            return {"ok": True, "name": name}

        engine = _engine(self.store, rollback=rb)
        row = await self._window(job_id=job.id)
        rec = await engine._maybe_rollback_failed_apply(
            row, JOBS.get(job.id), error="apt-get exit 1"
        )
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["snap_name"], "hlops-a")
        self.assertTrue(rec["via_agent"])
        self.assertEqual(rec["actor"], "Agent")
        self.assertEqual(calls, [("lxc:pve:1", "hlops-a")])
        again = await engine._maybe_rollback_failed_apply(
            row, JOBS.get(job.id), error="apt-get exit 1"
        )
        self.assertEqual(again["id"], rec["id"])
        self.assertEqual(len(calls), 1)
        reason = window_reason_after_rollback("apt-get exit 1", rec)
        self.assertIn("Zurückgesetzt", reason)
        self.assertIn(VIA_AGENT, reason)

    async def test_no_snap_skips_and_logs(self) -> None:
        job = JOBS.create(kind="image-apply", target_id="lxc:pve:1", via_agent=True)
        JOBS.finish(job.id, status="failed", error="pull failed")
        rb = AsyncMock()
        engine = _engine(self.store, rollback=rb)
        row = await self._window(job_id=job.id, bucket="images")
        rec = await engine._maybe_rollback_failed_apply(
            row, JOBS.get(job.id), error="pull failed"
        )
        self.assertEqual(rec["status"], "skipped")
        rb.assert_not_awaited()
        stored = await self.store.get_rollback_for_job(job.id)
        assert stored is not None
        self.assertEqual(stored["status"], "skipped")

    async def test_rollback_fail_audits_no_loop(self) -> None:
        job = JOBS.create(kind="apply", target_id="lxc:pve:1", via_agent=True)
        JOBS.finish(
            job.id,
            status="failed",
            error="Timeout",
            result={"snapshot": {"name": "hlops-b", "skipped": False}},
        )
        n = {"n": 0}

        async def rb(_tid: str, _name: str) -> dict:
            n["n"] += 1
            raise RuntimeError("LXC läuft noch")

        engine = _engine(self.store, rollback=rb)
        row = await self._window(job_id=job.id)
        rec = await engine._maybe_rollback_failed_apply(
            row, JOBS.get(job.id), error="Timeout"
        )
        self.assertEqual(rec["status"], "failed")
        self.assertIn("LXC", rec["error"])
        await engine._maybe_rollback_failed_apply(
            row, JOBS.get(job.id), error="Timeout"
        )
        self.assertEqual(n["n"], 1)

    async def test_sync_jobs_calls_rollback(self) -> None:
        job = JOBS.create(kind="apply", target_id="lxc:pve:1", via_agent=True)
        JOBS.finish(
            job.id,
            status="failed",
            error="ungesund nach Apply",
            result={"snapshot": {"name": "hlops-c", "skipped": False}},
        )
        calls: list[str] = []

        async def rb(_tid: str, name: str) -> dict:
            calls.append(name)
            return {"ok": True, "name": name}

        engine = _engine(self.store, rollback=rb)
        await self._window(job_id=job.id)
        await engine.sync_jobs()
        self.assertEqual(calls, ["hlops-c"])
        rows = await self.store.list_rollbacks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], REASON_UNHEALTHY)
        self.assertEqual(rows[0]["actor"], "Agent")
        self.assertTrue(rows[0]["via_agent"])
        win = (await self.store.list_windows(statuses=["failed"]))[0]
        self.assertIn("Zurückgesetzt", win["reason"])
        self.assertIn(VIA_AGENT, win["reason"])

    async def test_rate_limit_shifts_no_rollback_no_lesson(self) -> None:
        job = JOBS.create(kind="image-apply", target_id="lxc:pve:1", via_agent=True)
        JOBS.finish(
            job.id,
            status="failed",
            error="Error toomanyrequests: You have reached your unauthenticated pull rate limit.",
            result={"snapshot": {"skipped": False, "name": "hlops-mail"}},
        )
        rb = AsyncMock()
        engine = _engine(self.store, rollback=rb)
        row = await self._window(job_id=job.id, bucket="images")
        reason = await engine._fail_patch_window(
            row,
            job=JOBS.get(job.id),
            job_id=job.id,
            error="Error toomanyrequests: You have reached your unauthenticated pull rate limit.",
        )
        rb.assert_not_awaited()
        self.assertIn("Docker-Hub-Limit", reason)
        self.assertIn(VIA_AGENT, reason)
        fresh = await self.store.get_window(int(row["id"]))
        assert fresh is not None
        self.assertEqual(fresh["status"], STATUS_ACCEPTED)
        self.assertNotEqual(fresh["start_iso"], row["start_iso"])
        self.assertIn("Docker-Hub-Limit", fresh["reason"])
        self.assertEqual(len(await self.store.list_rollbacks()), 0)
        self.assertEqual(len(await self.store.list_lessons()), 0)
        settings = await self.store.get_settings()
        self.assertFalse(settings.get("patch_halted"))


if __name__ == "__main__":
    unittest.main()
