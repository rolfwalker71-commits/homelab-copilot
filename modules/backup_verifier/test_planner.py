"""Unit tests for backup gap scan and non-overlapping slot planner."""

from __future__ import annotations

import unittest

from backup_verifier.planner import (
    PlanError,
    classify_stacks,
    existing_start_minutes,
    format_hhmm,
    parse_hhmm,
    plan_slots,
    windows_overlap,
)


class ClassifyTests(unittest.TestCase):
    def test_scheduled_stack_not_missing(self) -> None:
        discovered = [
            {"parent_id": "lxc:pve:105", "stack": "paperless", "guest_name": "mail"},
            {"parent_id": "lxc:pve:105", "stack": "immich", "guest_name": "mail"},
        ]
        schedules = [
            {
                "parent_id": "lxc:pve:105",
                "stack": "paperless",
                "enabled": True,
                "cron_expr": "0 8 * * *",
            }
        ]
        result = classify_stacks(discovered, schedules)
        self.assertEqual([s["stack"] for s in result["missing"]], ["immich"])
        self.assertEqual([s["stack"] for s in result["scheduled"]], ["paperless"])

    def test_disabled_schedule_still_not_missing(self) -> None:
        discovered = [
            {"parent_id": "lxc:1", "stack": "wiki", "guest_name": "box"},
        ]
        schedules = [
            {
                "parent_id": "lxc:1",
                "stack": "wiki",
                "enabled": False,
                "cron_expr": "0 3 * * *",
            }
        ]
        result = classify_stacks(discovered, schedules)
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["scheduled"][0]["stack"], "wiki")

    def test_same_name_other_parent_is_missing(self) -> None:
        discovered = [
            {"parent_id": "lxc:a", "stack": "app", "guest_name": "a"},
            {"parent_id": "lxc:b", "stack": "app", "guest_name": "b"},
        ]
        schedules = [
            {"parent_id": "lxc:a", "stack": "app", "enabled": True, "cron_expr": "0 3 * * *"}
        ]
        result = classify_stacks(discovered, schedules)
        self.assertEqual(
            [(s["parent_id"], s["stack"]) for s in result["missing"]],
            [("lxc:b", "app")],
        )


class PlannerTests(unittest.TestCase):
    def test_skips_occupied_then_spaces_by_interval(self) -> None:
        plan = plan_slots(
            start_hm="08:00",
            interval_minutes=10,
            selected=[
                {"parent_id": "lxc:1", "stack": "a"},
                {"parent_id": "lxc:1", "stack": "b"},
                {"parent_id": "lxc:1", "stack": "c"},
            ],
            existing_starts=["08:00", "08:10"],
        )
        self.assertEqual([p["start_hm"] for p in plan], ["08:20", "08:30", "08:40"])
        self.assertTrue(plan[0]["shifted"])
        self.assertEqual(plan[0]["skipped_slots"], 2)
        self.assertFalse(plan[1]["shifted"])

    def test_free_start_uses_given_time(self) -> None:
        plan = plan_slots(
            start_hm="08:00",
            interval_minutes=10,
            selected=[
                {"parent_id": "lxc:1", "stack": "a"},
                {"parent_id": "lxc:1", "stack": "b"},
            ],
            existing_starts=[],
        )
        self.assertEqual([p["start_hm"] for p in plan], ["08:00", "08:10"])
        self.assertFalse(any(p["wrapped"] for p in plan))

    def test_off_grid_existing_shifts_new_job(self) -> None:
        """08:00 and 08:10 both overlap [08:05, 08:15) → first free is 08:20."""
        plan = plan_slots(
            start_hm="08:00",
            interval_minutes=10,
            selected=[{"parent_id": "lxc:1", "stack": "a"}],
            existing_starts=["08:05"],
        )
        self.assertEqual(plan[0]["start_hm"], "08:20")

    def test_new_jobs_never_share_a_minute(self) -> None:
        plan = plan_slots(
            start_hm="09:00",
            interval_minutes=10,
            selected=[
                {"parent_id": "lxc:1", "stack": "a"},
                {"parent_id": "lxc:1", "stack": "b"},
                {"parent_id": "lxc:1", "stack": "c"},
            ],
            existing_starts=[],
        )
        times = [p["start_hm"] for p in plan]
        self.assertEqual(len(times), len(set(times)))

    def test_wraps_past_midnight_to_next_day_clock(self) -> None:
        """23:50 occupied → 00:00, 00:10, 00:20 (rolled to next calendar day)."""
        plan = plan_slots(
            start_hm="23:50",
            interval_minutes=10,
            selected=[
                {"parent_id": "lxc:1", "stack": "a"},
                {"parent_id": "lxc:1", "stack": "b"},
                {"parent_id": "lxc:1", "stack": "c"},
            ],
            existing_starts=["23:50"],
        )
        self.assertEqual([p["start_hm"] for p in plan], ["00:00", "00:10", "00:20"])
        self.assertTrue(plan[0]["wrapped"])
        self.assertTrue(plan[1]["wrapped"])

    def test_empty_selection_rejected(self) -> None:
        with self.assertRaises(PlanError):
            plan_slots(start_hm="08:00", selected=[], existing_starts=[])

    def test_full_day_packed_raises(self) -> None:
        existing = [format_hhmm(m) for m in range(0, 24 * 60, 10)]
        with self.assertRaises(PlanError) as ctx:
            plan_slots(
                start_hm="08:00",
                interval_minutes=10,
                selected=[{"parent_id": "lxc:1", "stack": "a"}],
                existing_starts=existing,
            )
        self.assertIn("24 Stunden", ctx.exception.message)


class OccupiedSlotTests(unittest.TestCase):
    def test_disabled_schedule_does_not_occupy(self) -> None:
        mins = existing_start_minutes(
            [
                {
                    "enabled": False,
                    "start_hm": "08:00",
                    "cron_expr": "0 8 * * *",
                },
                {
                    "enabled": True,
                    "start_hm": "08:10",
                    "cron_expr": "10 8 * * *",
                },
            ]
        )
        self.assertEqual(mins, [8 * 60 + 10])


class WindowTests(unittest.TestCase):
    def test_back_to_back_does_not_overlap(self) -> None:
        self.assertFalse(windows_overlap(parse_hhmm("08:00"), parse_hhmm("08:10"), 10))

    def test_same_minute_overlaps(self) -> None:
        self.assertTrue(windows_overlap(parse_hhmm("08:00"), parse_hhmm("08:00"), 10))

    def test_midnight_wrap_overlaps(self) -> None:
        self.assertTrue(windows_overlap(parse_hhmm("23:55"), parse_hhmm("00:00"), 10))
        self.assertFalse(windows_overlap(parse_hhmm("23:50"), parse_hhmm("00:00"), 10))


if __name__ == "__main__":
    unittest.main()
