"""Unit tests for snapshot name validation and auto-retention."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.core.snapshots import (
    AUTO_PREFIX,
    SnapshotNameError,
    auto_snap_name,
    clamp_keep,
    guest_can_snapshot,
    is_auto_snap,
    snaps_to_delete,
    validate_snap_name,
)


class NameTests(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(validate_snap_name("pre-patch"), "pre-patch")
        self.assertEqual(validate_snap_name("hlops-20260101-120000"), "hlops-20260101-120000")

    def test_rejects_current_and_junk(self) -> None:
        with self.assertRaises(SnapshotNameError):
            validate_snap_name("current")
        with self.assertRaises(SnapshotNameError):
            validate_snap_name("1starts-digit")
        with self.assertRaises(SnapshotNameError):
            validate_snap_name("has space")
        with self.assertRaises(SnapshotNameError):
            validate_snap_name("")

    def test_auto_name_prefix(self) -> None:
        now = datetime(2026, 9, 5, 15, 6, 7, tzinfo=timezone.utc)
        name = auto_snap_name(now)
        self.assertTrue(name.startswith(AUTO_PREFIX))
        self.assertTrue(is_auto_snap(name))
        self.assertFalse(is_auto_snap("current"))
        self.assertFalse(is_auto_snap("manual-before-upgrade"))


class RetentionTests(unittest.TestCase):
    def test_keeps_last_n_auto_only(self) -> None:
        snaps = [
            {"name": "current", "current": True, "snaptime": 9},
            {"name": "manual-keep", "snaptime": 8},
            {"name": "hlops-3", "snaptime": 3},
            {"name": "hlops-1", "snaptime": 1},
            {"name": "hlops-2", "snaptime": 2},
            {"name": "hlops-4", "snaptime": 4},
        ]
        gone = snaps_to_delete(snaps, keep=2)
        self.assertEqual(gone, ["hlops-2", "hlops-1"])

    def test_keep_all_when_under_limit(self) -> None:
        snaps = [
            {"name": "hlops-a", "snaptime": 10},
            {"name": "hlops-b", "snaptime": 11},
        ]
        self.assertEqual(snaps_to_delete(snaps, keep=3), [])

    def test_clamp_keep(self) -> None:
        self.assertEqual(clamp_keep(None), 3)
        self.assertEqual(clamp_keep(0), 1)
        self.assertEqual(clamp_keep(99), 50)

    def test_guest_can_snapshot(self) -> None:
        self.assertTrue(guest_can_snapshot("lxc:pve01:105"))
        self.assertTrue(guest_can_snapshot("qemu:pve01:200"))
        self.assertFalse(guest_can_snapshot("node:pve01"))
        self.assertFalse(guest_can_snapshot("pve01"))
        self.assertFalse(guest_can_snapshot("manual:3"))


if __name__ == "__main__":
    unittest.main()
