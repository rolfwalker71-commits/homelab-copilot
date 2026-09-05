"""Unit tests for compose-stack backup listing (restic + tar, no guest SSH)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.locale import BERLIN

from backup_verifier.stack_backups import (
    CHECK_URL,
    assert_stack_keys,
    cache_clear,
    cache_get,
    cache_put,
    dir_size_bytes,
    last_job_payload,
    list_tar_archives,
    parse_restic_time,
    schedule_payload,
    shape_restic_items,
    snapshot_kind,
    StackBackupError,
    where_payload,
)


class KeyTests(unittest.TestCase):
    def test_accepts_lxc_parent(self) -> None:
        parent, project = assert_stack_keys("lxc:pve01:105", "aicrochetmaster")
        self.assertEqual(parent, "lxc:pve01:105")
        self.assertEqual(project, "aicrochetmaster")

    def test_rejects_traversal(self) -> None:
        with self.assertRaises(StackBackupError):
            assert_stack_keys("../etc", "stack")
        with self.assertRaises(StackBackupError):
            assert_stack_keys("lxc:pve:1", "..")
        with self.assertRaises(StackBackupError):
            assert_stack_keys("lxc/pve/1", "stack")
        with self.assertRaises(StackBackupError):
            assert_stack_keys("", "stack")


class SnapshotShapeTests(unittest.TestCase):
    def test_kind_from_tags(self) -> None:
        self.assertEqual(snapshot_kind(["homelab-copilot", "stack:x", "full"]), "full")
        self.assertEqual(snapshot_kind(["incr"]), "incr")
        self.assertEqual(snapshot_kind(["stack:x"]), "")

    def test_restic_time_berlin(self) -> None:
        dt = parse_restic_time("2026-09-05T14:02:11.123+02:00")
        self.assertIsNotNone(dt)
        assert dt is not None
        self.assertEqual(dt.astimezone(BERLIN).hour, 14)

    def test_size_from_matching_run(self) -> None:
        snaps = [
            {
                "id": "aabbccddeeff00112233445566778899",
                "short_id": "aabbccdd",
                "time": "2026-09-05T12:00:00+00:00",
                "tags": ["full", "stack:demo"],
            }
        ]
        runs = [
            {
                "snapshot_id": "aabbccddeeff00112233445566778899",
                "bytes_added": 12_582_912,
            }
        ]
        items = shape_restic_items(snaps, runs)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "full")
        self.assertEqual(items[0]["kind_label"], "Voll")
        self.assertEqual(items[0]["size_bytes"], 12_582_912)
        self.assertIn("MiB", items[0]["size"] or "")
        self.assertTrue(items[0]["time"])
        self.assertNotIn("homelab-copilot", items[0]["tags"])


class TarListTests(unittest.TestCase):
    def test_lists_archives_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "aicrochetmaster"
            folder.mkdir()
            older = folder / "aicrochetmaster_20260101_010101.tar.gz"
            newer = folder / "aicrochetmaster_20260905_140211.tar.gz"
            older.write_bytes(b"old")
            newer.write_bytes(b"new" * 100)
            older_ts = datetime(2026, 1, 1, 1, 1, 1, tzinfo=ZoneInfo("UTC")).timestamp()
            newer_ts = datetime(2026, 9, 5, 12, 2, 11, tzinfo=ZoneInfo("UTC")).timestamp()
            os_utime = __import__("os").utime
            os_utime(older, (older_ts, older_ts))
            os_utime(newer, (newer_ts, newer_ts))
            items = list_tar_archives(root, "aicrochetmaster")
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0]["name"], newer.name)
            self.assertEqual(items[0]["engine"], "tar")
            self.assertEqual(items[0]["size_bytes"], 300)
            self.assertTrue(items[0]["time"])

    def test_empty_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(list_tar_archives(Path(tmp), "missing"), [])


class DirSizeTests(unittest.TestCase):
    def test_sums_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").write_bytes(b"12345")
            (root / "sub").mkdir()
            (root / "sub" / "b").write_bytes(b"abc")
            self.assertEqual(dir_size_bytes(root), 8)


class LastJobAndScheduleTests(unittest.TestCase):
    def test_last_job_labels(self) -> None:
        job = last_job_payload(
            {
                "id": 9,
                "status": "success",
                "engine": "restic",
                "created_at": "05.09.2026, 14:02:00 Uhr",
                "size_bytes": 100,
            }
        )
        assert job is not None
        self.assertEqual(job["status_label"], "OK")
        self.assertEqual(job["engine"], "restic")
        self.assertIsNone(last_job_payload(None))

    def test_schedule_next_and_retention(self) -> None:
        row = {
            "id": 3,
            "enabled": True,
            "engine": "restic",
            "cron_expr": "0 3 * * *",
            "restic_keep_last": 14,
            "restic_keep_weekly": 8,
            "restic_full_every_days": 7,
        }
        payload = schedule_payload(row)
        assert payload is not None
        self.assertEqual(payload["engine_label"], "Incremental (restic)")
        self.assertEqual(payload["keep_last"], 14)
        self.assertEqual(payload["keep_weekly"], 8)
        self.assertEqual(payload["full_every_days"], 7)
        self.assertEqual(payload["check_url"], CHECK_URL)
        self.assertTrue(payload["next_run"])

    def test_no_schedule(self) -> None:
        self.assertIsNone(schedule_payload(None))


class WhereTests(unittest.TestCase):
    def test_copilot_and_hetzner_last_hop(self) -> None:
        dests = [
            {"kind": "copilot", "enabled": True, "label": "Copilot"},
            {
                "kind": "sftp",
                "enabled": True,
                "label": "Hetzner",
                "preset": "storage_box",
                "host": "u123.your-storagebox.de",
            },
        ]
        run = {
            "destinations": [
                {"kind": "copilot", "status": "ok"},
                {"kind": "sftp", "status": "ok", "preset": "storage_box"},
            ]
        }
        where = where_payload(dests, run, copilot_restic=True, copilot_tar=False)
        self.assertTrue(where["copilot"]["present"])
        self.assertTrue(where["dest"]["hetzner"])
        self.assertTrue(where["dest"]["present"])
        self.assertEqual(where["dest"]["last_hop"], "ok")
        self.assertEqual(where["dest"]["label"], "Hetzner")

    def test_dest_unknown_without_hop(self) -> None:
        dests = [
            {
                "kind": "sftp",
                "enabled": True,
                "label": "Box",
                "preset": "storage_box",
                "host": "u123.your-storagebox.de",
            }
        ]
        where = where_payload(dests, None, copilot_restic=False, copilot_tar=False)
        self.assertFalse(where["copilot"]["present"])
        self.assertIsNone(where["dest"]["present"])
        self.assertTrue(where["dest"]["configured"])


class CacheTests(unittest.TestCase):
    def tearDown(self) -> None:
        cache_clear()

    def test_roundtrip(self) -> None:
        cache_clear()
        self.assertIsNone(cache_get("lxc:1", "demo"))
        cache_put("lxc:1", "demo", {"ok": True, "items": []})
        hit = cache_get("lxc:1", "demo")
        assert hit is not None
        self.assertTrue(hit["ok"])


if __name__ == "__main__":
    unittest.main()
