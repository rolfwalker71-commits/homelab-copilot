"""Compose-stack schedules cover guests; LXCs without stacks are not nags."""

from __future__ import annotations

import unittest

from ops_agent.backup_coverage import (
    host_needs_backup_nag,
    schedule_covers_host,
)
from ops_agent.ledger import STATUS_NO_PLAN, STATUS_QUEUED, build_day_ledger


class CoverageTests(unittest.TestCase):
    def test_rustdesk_schedule_covers_guest_by_name(self) -> None:
        sched = {
            "parent_id": "lxc:pve01:116",
            "stack": "rustdesk",
            "guest_name": "rustdesk",
            "enabled": True,
            "cron_expr": "0 3 * * *",
        }
        host = {"id": "lxc:pve01:116", "name": "rustdesk", "kind": "lxc"}
        self.assertTrue(schedule_covers_host(sched, host))
        self.assertTrue(
            schedule_covers_host(sched, {"id": "other", "name": "rustdesk"})
        )
        self.assertFalse(schedule_covers_host(sched, {"id": "lxc:x", "name": "haos"}))

    def test_haos_without_stack_is_not_nagged(self) -> None:
        self.assertFalse(
            host_needs_backup_nag(
                {"id": "qemu:pve01:160", "name": "haos"},
                [],
                [],
            )
        )

    def test_immich_stack_without_schedule_is_nagged(self) -> None:
        self.assertTrue(
            host_needs_backup_nag(
                {"id": "lxc:pve01:120", "name": "immich"},
                [{"parent_id": "lxc:pve01:120", "stack": "immich", "guest_name": "immich"}],
                [],
            )
        )

    def test_rustdesk_with_cron_is_not_nagged(self) -> None:
        self.assertFalse(
            host_needs_backup_nag(
                {"id": "lxc:pve01:116", "name": "rustdesk"},
                [{"parent_id": "lxc:pve01:116", "stack": "rustdesk", "guest_name": "rustdesk"}],
                [
                    {
                        "parent_id": "lxc:pve01:116",
                        "stack": "rustdesk",
                        "guest_name": "rustdesk",
                        "enabled": True,
                    }
                ],
            )
        )


class LedgerCoverageTests(unittest.TestCase):
    def test_schedule_row_not_kein_plan(self) -> None:
        ledger = build_day_ledger(
            schedules=[
                {
                    "id": 1,
                    "parent_id": "lxc:pve01:116",
                    "stack": "rustdesk",
                    "guest_name": "rustdesk",
                    "enabled": True,
                    "cron_expr": "0 3 * * *",
                }
            ],
            hosts=[{"id": "lxc:pve01:116", "name": "rustdesk", "kind": "lxc"}],
            backup_stacks=[
                {"parent_id": "lxc:pve01:116", "stack": "rustdesk", "guest_name": "rustdesk"}
            ],
        )
        self.assertEqual(len(ledger["backups"]), 1)
        self.assertEqual(ledger["backups"][0]["status"], STATUS_QUEUED)
        self.assertEqual(ledger["backups"][0]["soll_hm"], "03:00")

    def test_guests_without_stacks_omitted(self) -> None:
        ledger = build_day_ledger(
            hosts=[
                {"id": "lxc:adguard", "name": "adguard", "kind": "lxc"},
                {"id": "qemu:haos", "name": "haos", "kind": "qemu"},
                {"id": "lxc:ittools", "name": "ittools", "kind": "lxc"},
            ],
            backup_stacks=[],
            prompts=[
                {
                    "kind": "no_backup",
                    "target_id": "qemu:haos",
                    "target_name": "haos",
                    "reason": "haos hat keinen Backup-Plan.",
                }
            ],
        )
        self.assertEqual(ledger["backups"], [])

    def test_immich_unscheduled_stack_is_no_plan(self) -> None:
        ledger = build_day_ledger(
            hosts=[{"id": "lxc:immich", "name": "immich", "kind": "lxc"}],
            backup_stacks=[
                {"parent_id": "lxc:immich", "stack": "immich", "guest_name": "immich"}
            ],
        )
        self.assertEqual(ledger["backups"][0]["status"], STATUS_NO_PLAN)
        self.assertIn("immich", ledger["backups"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
