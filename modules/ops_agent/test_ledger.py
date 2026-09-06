"""Soll/Ist ledger — ohne LLM, ohne Proxmox."""

from __future__ import annotations

import unittest
from datetime import datetime

from app.core.locale import BERLIN, iso_utc
from ops_agent.ledger import (
    STATUS_EMPTY,
    STATUS_NO_PLAN,
    STATUS_OK,
    STATUS_QUEUED,
    build_day_ledger,
)
from ops_agent.planner import KIND_BACKUP, KIND_PATCH, STATUS_DONE
from patcher.agent import HostPending


def _now() -> datetime:
    return datetime(2026, 9, 6, 18, 20, tzinfo=BERLIN)


class LedgerTests(unittest.TestCase):
    def test_backup_soll_ist_ok(self) -> None:
        now = _now()
        start = now.replace(hour=20, minute=0, second=0, microsecond=0)
        ledger = build_day_ledger(
            now=now,
            schedules=[
                {
                    "id": 1,
                    "parent_id": "lxc:1",
                    "stack": "weatherapp",
                    "guest_name": "mail",
                    "enabled": True,
                    "cron_expr": "0 20 * * *",
                }
            ],
            windows=[
                {
                    "kind": KIND_BACKUP,
                    "target_id": "lxc:1",
                    "stack": "weatherapp",
                    "schedule_id": 1,
                    "status": STATUS_DONE,
                    "start_iso": iso_utc(start),
                    "start_hm": "20:00",
                    "updated_at_iso": iso_utc(now),
                }
            ],
            runs=[],
            hosts=[],
        )
        row = ledger["backups"][0]
        self.assertEqual(row["soll_hm"], "20:00")
        self.assertEqual(row["status"], STATUS_OK)
        self.assertEqual(row["ist_hm"], "18:20")

    def test_immich_without_schedule_is_no_plan(self) -> None:
        ledger = build_day_ledger(
            now=_now(),
            schedules=[],
            windows=[],
            hosts=[{"id": "lxc:immich", "name": "immich", "kind": "lxc"}],
            prompts=[
                {
                    "kind": "no_backup",
                    "target_id": "lxc:immich",
                    "target_name": "immich",
                    "reason": "immich hat keinen Backup-Plan. So gewollt?",
                }
            ],
        )
        self.assertEqual(ledger["backups"][0]["status"], STATUS_NO_PLAN)
        self.assertIn("keinen Backup-Plan", ledger["backups"][0]["reason"])

    def test_ticked_host_without_scan_is_empty_reason(self) -> None:
        ledger = build_day_ledger(
            now=_now(),
            hosts=[{"id": "lxc:1", "name": "mail", "kind": "lxc"}],
            patch_scope_ids=["lxc:1"],
            image_scope_ids=["lxc:1"],
            pending=[],
        )
        self.assertEqual(ledger["patches"][0]["status"], STATUS_EMPTY)
        self.assertIn("nichts offen", ledger["patches"][0]["reason"])
        self.assertEqual(ledger["images"][0]["status"], STATUS_EMPTY)
        self.assertIn("nichts offen", ledger["images"][0]["reason"])

    def test_pending_but_not_in_matrix(self) -> None:
        ledger = build_day_ledger(
            now=_now(),
            hosts=[{"id": "lxc:1", "name": "mail", "kind": "lxc"}],
            patch_scope_ids=[],
            pending=[
                HostPending(
                    target_id="lxc:1",
                    target_name="mail",
                    packages=[{"name": "openssl", "priority": "security"}],
                )
            ],
        )
        self.assertIn("nicht in Matrix", ledger["patches"][0]["reason"])

    def test_queued_backup_still_shows_soll(self) -> None:
        ledger = build_day_ledger(
            now=_now(),
            schedules=[
                {
                    "id": 2,
                    "parent_id": "lxc:1",
                    "stack": "paperless",
                    "guest_name": "mail",
                    "enabled": True,
                    "cron_expr": "0 20 * * *",
                }
            ],
        )
        self.assertEqual(ledger["backups"][0]["status"], STATUS_QUEUED)
        self.assertEqual(ledger["backups"][0]["soll_hm"], "20:00")


if __name__ == "__main__":
    unittest.main()
