"""Unit tests for snapshot name validation and auto-retention."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.core.snapshots import (
    AUTO_PREFIX,
    SnapshotNameError,
    auto_snap_name,
    build_snapshot_tree,
    can_rollback_snap,
    clamp_keep,
    guest_can_snapshot,
    guest_kind,
    is_auto_snap,
    is_current_marker,
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
        self.assertEqual(guest_kind("lxc:pve01:105"), "lxc")
        self.assertEqual(guest_kind("qemu:pve01:200"), "qemu")
        self.assertEqual(guest_kind("node:pve01"), "")


class TreeTests(unittest.TestCase):
    def _names(self, tree: list) -> list[str]:
        return [row["name"] for row in tree]

    def test_parent_child_and_current_marker(self) -> None:
        snaps = [
            {"name": "current", "parent": "hlops-20260905-161823"},
            {
                "name": "hlops-20260905-161823",
                "parent": "pre-upgrade",
                "snaptime": 200,
            },
            {"name": "pre-upgrade", "snaptime": 100, "description": "Wurzel"},
        ]
        tree = build_snapshot_tree(snaps)
        self.assertEqual(
            self._names(tree),
            ["pre-upgrade", "hlops-20260905-161823", "current"],
        )
        self.assertEqual(tree[0]["depth"], 0)
        self.assertEqual(tree[0]["relation"], "wurzel")
        self.assertTrue(tree[0]["is_root"])
        self.assertTrue(tree[0]["can_rollback"])
        self.assertEqual(tree[1]["depth"], 1)
        self.assertEqual(tree[1]["relation"], "kind")
        self.assertEqual(tree[1]["parent"], "pre-upgrade")
        self.assertTrue(tree[1]["active"])
        self.assertTrue(tree[1]["can_rollback"])
        self.assertEqual(tree[2]["depth"], 2)
        self.assertEqual(tree[2]["relation"], "aktuell")
        self.assertTrue(tree[2]["current"])
        self.assertFalse(tree[2]["can_rollback"])
        self.assertFalse(can_rollback_snap(tree[2]))
        self.assertTrue(is_current_marker(tree[2]))

    def test_missing_parent_becomes_root(self) -> None:
        snaps = [
            {"name": "orphan", "parent": "deleted-snap", "snaptime": 5},
            {"name": "other", "snaptime": 1},
        ]
        tree = build_snapshot_tree(snaps)
        by_name = {row["name"]: row for row in tree}
        self.assertEqual(by_name["orphan"]["depth"], 0)
        self.assertTrue(by_name["orphan"]["is_root"])
        self.assertEqual(by_name["orphan"]["relation"], "wurzel")
        self.assertIsNone(by_name["orphan"]["parent"])
        self.assertEqual(by_name["other"]["depth"], 0)
        self.assertEqual(self._names(tree), ["other", "orphan"])

    def test_cycle_breaks_into_roots(self) -> None:
        snaps = [
            {"name": "a", "parent": "b", "snaptime": 1},
            {"name": "b", "parent": "a", "snaptime": 2},
        ]
        tree = build_snapshot_tree(snaps)
        self.assertEqual(len(tree), 2)
        self.assertTrue(all(row["depth"] == 0 for row in tree))
        self.assertTrue(all(row["is_root"] for row in tree))
        self.assertEqual(set(self._names(tree)), {"a", "b"})

    def test_self_parent_is_root(self) -> None:
        snaps = [{"name": "loop", "parent": "loop", "snaptime": 3}]
        tree = build_snapshot_tree(snaps)
        self.assertEqual(len(tree), 1)
        self.assertEqual(tree[0]["depth"], 0)
        self.assertTrue(tree[0]["is_root"])
        self.assertIsNone(tree[0]["parent"])

    def test_flat_list_no_parents(self) -> None:
        snaps = [
            {"name": "hlops-b", "snaptime": 20},
            {"name": "hlops-a", "snaptime": 10},
            {"name": "current", "current": True},
        ]
        tree = build_snapshot_tree(snaps)
        self.assertEqual(self._names(tree), ["hlops-a", "hlops-b", "current"])
        self.assertTrue(all(row["depth"] == 0 for row in tree))
        self.assertFalse(tree[0]["current"])
        self.assertTrue(tree[0]["can_rollback"])
        self.assertTrue(tree[2]["current"])
        self.assertFalse(tree[2]["can_rollback"])

    def test_three_node_cycle(self) -> None:
        snaps = [
            {"name": "a", "parent": "c", "snaptime": 1},
            {"name": "b", "parent": "a", "snaptime": 2},
            {"name": "c", "parent": "b", "snaptime": 3},
        ]
        tree = build_snapshot_tree(snaps)
        self.assertEqual(len(tree), 3)
        self.assertTrue(all(row["depth"] == 0 for row in tree))


if __name__ == "__main__":
    unittest.main()
