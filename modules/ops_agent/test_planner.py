"""Ops-Agent: Konflikte, Überlauf, Offline, Ingest, Policy — ohne LLM."""

from __future__ import annotations

import unittest
from datetime import datetime

from app.core.locale import BERLIN
from ops_agent.planner import (
    KIND_BACKUP,
    KIND_PATCH,
    Occupied,
    PlannedWindow,
    REASON_BACKUP_OVERRUN,
    REASON_HOST_OFFLINE,
    STATUS_ACCEPTED,
    STATUS_SKIPPED,
    STATUS_WAITING,
    detect_overrun_shift,
    ingest_schedule_windows,
    occupied_from_windows,
    propose_windows,
    Need,
)
from ops_agent.policy import ConfirmPolicy, default_policy, needs_human


def _now() -> datetime:
    return datetime(2026, 9, 6, 18, 0, tzinfo=BERLIN)


class PolicyTests(unittest.TestCase):
    def test_default_security_and_backup_autonomous(self) -> None:
        p = default_policy()
        wait, reasons = needs_human(
            p,
            kind="patch",
            bucket="security",
            confirm_reasons=[],
            known_host=True,
        )
        self.assertFalse(wait)
        self.assertEqual(reasons, [])
        wait_b, _ = needs_human(
            p,
            kind="backup",
            has_existing_schedule=True,
            known_host=True,
        )
        self.assertFalse(wait_b)

    def test_kernel_waits_by_default(self) -> None:
        wait, reasons = needs_human(
            default_policy(),
            kind="patch",
            bucket="regular",
            confirm_reasons=["kernel"],
        )
        self.assertTrue(wait)
        self.assertIn("kernel-docker", reasons)

    def test_new_guest_backup_waits(self) -> None:
        wait, reasons = needs_human(
            default_policy(),
            kind="backup",
            has_existing_schedule=False,
        )
        self.assertTrue(wait)
        self.assertIn("erstes-backup", reasons)

    def test_confirm_nothing_skips_wait(self) -> None:
        p = ConfirmPolicy(confirm_nothing=True, confirm_kernel_docker=True)
        wait, _ = needs_human(
            p, kind="patch", bucket="regular", confirm_reasons=["kernel"]
        )
        self.assertFalse(wait)

    def test_german_default_note(self) -> None:
        note = default_policy().to_dict()["defaults_note"]
        self.assertIn("Kernel", note)
        self.assertIn("DistUpgrade", note)


class ConflictTests(unittest.TestCase):
    def test_backup_and_patch_same_host_do_not_overlap(self) -> None:
        occupied = [
            Occupied(
                target_id="lxc:pve:105",
                kind=KIND_BACKUP,
                start_min=20 * 60,
                duration_min=10,
                global_backup=True,
            )
        ]
        needs = [
            Need(
                kind=KIND_PATCH,
                target_id="lxc:pve:105",
                target_name="mail",
                bucket="security",
                duration_min=20,
                known_host=True,
            )
        ]
        planned, skipped = propose_windows(
            needs, occupied, now=_now(), policy=default_policy()
        )
        self.assertEqual(skipped, [])
        self.assertEqual(len(planned), 1)
        patch = planned[0]
        self.assertGreaterEqual(patch.start_min, 20 * 60 + 10)
        self.assertEqual(patch.start_hm, "20:10")
        self.assertEqual(patch.status, STATUS_ACCEPTED)

    def test_two_patches_serialize(self) -> None:
        needs = [
            Need(
                kind=KIND_PATCH,
                target_id="lxc:1",
                target_name="a",
                bucket="security",
                duration_min=20,
            ),
            Need(
                kind=KIND_PATCH,
                target_id="lxc:2",
                target_name="b",
                bucket="security",
                duration_min=20,
            ),
        ]
        planned, _ = propose_windows(
            needs, [], now=_now(), policy=default_policy()
        )
        self.assertEqual(len(planned), 2)
        self.assertLessEqual(
            planned[0].start_min + planned[0].duration_min, planned[1].start_min
        )


class OverrunTests(unittest.TestCase):
    def test_running_backup_shifts_patch_with_german_reason(self) -> None:
        now = datetime(2026, 9, 6, 20, 15, tzinfo=BERLIN)
        running = Occupied(
            target_id="lxc:1",
            kind=KIND_BACKUP,
            start_min=20 * 60,
            duration_min=40,
            global_backup=True,
        )
        later = PlannedWindow(
            kind=KIND_PATCH,
            target_id="lxc:1",
            target_name="web",
            bucket="security",
            start_min=20 * 60 + 20,
            start_hm="20:20",
            duration_min=20,
            status=STATUS_ACCEPTED,
        )
        occupied = [running, Occupied(
            target_id="lxc:1",
            kind=KIND_PATCH,
            start_min=later.start_min,
            duration_min=20,
        )]
        result = detect_overrun_shift(
            running=running, later=later, occupied=occupied, now=now
        )
        self.assertIsNotNone(result)
        moved, shift = result
        self.assertGreaterEqual(moved.start_min, 20 * 60 + 40)
        self.assertEqual(shift["old_start_hm"], "20:20")
        self.assertEqual(shift["new_start_hm"], moved.start_hm)
        self.assertEqual(shift["reason"], REASON_BACKUP_OVERRUN)
        self.assertIn("Backup", shift["reason"])


class OfflineTests(unittest.TestCase):
    def test_offline_host_skipped_with_reason(self) -> None:
        needs = [
            Need(
                kind=KIND_PATCH,
                target_id="lxc:9",
                target_name="down",
                bucket="security",
                duration_min=20,
            )
        ]
        planned, skipped = propose_windows(
            needs,
            [],
            now=_now(),
            policy=default_policy(),
            host_online={"lxc:9": False},
        )
        self.assertEqual(planned, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].status, STATUS_SKIPPED)
        self.assertEqual(skipped[0].reason, REASON_HOST_OFFLINE)


class IngestTests(unittest.TestCase):
    def test_existing_schedules_appear_on_board(self) -> None:
        schedules = [
            {
                "id": 7,
                "parent_id": "lxc:pve:105",
                "stack": "paperless",
                "guest_name": "mail",
                "enabled": True,
                "start_hm": "03:00",
                "engine": "restic",
            }
        ]
        windows = ingest_schedule_windows(schedules, now=_now(), drill_enabled=False)
        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.stack, "paperless")
        self.assertEqual(w.start_hm, "03:00")
        self.assertEqual(w.source, "ingested")
        self.assertEqual(w.status, STATUS_ACCEPTED)
        self.assertEqual(w.schedule_id, 7)
        self.assertIn("Zeitplan", w.reason)
        occ = occupied_from_windows(windows)
        self.assertTrue(occ[0].global_backup)


class WaitingTests(unittest.TestCase):
    def test_kernel_window_is_waiting_not_accepted(self) -> None:
        needs = [
            Need(
                kind=KIND_PATCH,
                target_id="lxc:1",
                target_name="web",
                bucket="regular",
                duration_min=30,
                confirm_reasons=["kernel"],
            )
        ]
        planned, _ = propose_windows(
            needs, [], now=_now(), policy=default_policy()
        )
        self.assertEqual(planned[0].status, STATUS_WAITING)
        self.assertTrue(planned[0].needs_confirm)
        self.assertIn("Kernel", planned[0].reason)


if __name__ == "__main__":
    unittest.main()
