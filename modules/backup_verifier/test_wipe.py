"""Unit tests for backup wipe keyword check and path jail."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backup_verifier.wipe import (
    WipeError,
    assert_copilot_wipe_root,
    assert_safe_wipe_path,
    normalize_wipe_path,
    validate_wipe_keyword,
    wipe_local_dir_contents,
)


class KeywordTests(unittest.TestCase):
    def test_exact_match_with_umlaut(self) -> None:
        validate_wipe_keyword("LÖSCH-A1B2C3", "LÖSCH-A1B2C3")

    def test_mismatch_rejected(self) -> None:
        with self.assertRaises(WipeError) as ctx:
            validate_wipe_keyword("LOSCH-A1B2C3", "LÖSCH-A1B2C3")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_empty_and_none_rejected(self) -> None:
        with self.assertRaises(WipeError):
            validate_wipe_keyword("", "LÖSCH-AABBCC")
        with self.assertRaises(WipeError):
            validate_wipe_keyword(None, "LÖSCH-AABBCC")
        with self.assertRaises(WipeError):
            validate_wipe_keyword("LÖSCH-AABBCC", None)
        with self.assertRaises(WipeError):
            validate_wipe_keyword("LÖSCH-AABBCC", "")

    def test_whitespace_is_not_stripped_for_match(self) -> None:
        with self.assertRaises(WipeError):
            validate_wipe_keyword(" LÖSCH-A1B2C3", "LÖSCH-A1B2C3")

    def test_expired_token(self) -> None:
        with self.assertRaises(WipeError) as ctx:
            validate_wipe_keyword(
                "LÖSCH-A1B2C3",
                "LÖSCH-A1B2C3",
                issued_at=0.0,
                now=10000.0,
                ttl_s=60.0,
            )
        self.assertIn("abgelaufen", ctx.exception.message)

    def test_fresh_token_ok(self) -> None:
        validate_wipe_keyword(
            "LÖSCH-A1B2C3",
            "LÖSCH-A1B2C3",
            issued_at=100.0,
            now=120.0,
            ttl_s=60.0,
        )


class PathSafetyTests(unittest.TestCase):
    def test_reject_system_roots(self) -> None:
        for raw in ("/", "/data", "/home", "/var", "/tmp", "/root", ".", "", ".."):
            with self.subTest(raw=raw):
                with self.assertRaises(WipeError):
                    assert_safe_wipe_path(raw)

    def test_reject_dotdot(self) -> None:
        with self.assertRaises(WipeError):
            normalize_wipe_path("/data/backups/../etc")
        with self.assertRaises(WipeError):
            assert_safe_wipe_path("/data/backups/foo/../../etc")

    def test_reject_data_dir_even_if_allowed(self) -> None:
        with self.assertRaises(WipeError):
            assert_safe_wipe_path("/data", allowed_root="/data", data_dir="/data")

    def test_accept_copilot_backups(self) -> None:
        out = assert_safe_wipe_path(
            "/data/backups", allowed_root="/data/backups", data_dir="/data"
        )
        self.assertEqual(out, "/data/backups")

    def test_accept_dest_subdir_not_home(self) -> None:
        out = assert_safe_wipe_path(
            "/home/u123456/backups", allowed_root="/home/u123456/backups"
        )
        self.assertEqual(out, "/home/u123456/backups")
        with self.assertRaises(WipeError):
            assert_safe_wipe_path("/home", allowed_root="/home")

    def test_reject_path_outside_allowed_root(self) -> None:
        with self.assertRaises(WipeError):
            assert_safe_wipe_path("/data/other", allowed_root="/data/backups")

    def test_copilot_root_must_not_be_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            with self.assertRaises(WipeError):
                assert_copilot_wipe_root(data, data)
            backups = data / "backups"
            backups.mkdir()
            self.assertEqual(assert_copilot_wipe_root(backups, data), backups.resolve())


class LocalWipeTests(unittest.TestCase):
    def test_deletes_children_keeps_root_and_db_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            root = data / "backups"
            root.mkdir(parents=True)
            (root / "paperless").mkdir()
            (root / "paperless" / "a.tar.gz").write_text("x", encoding="utf-8")
            (root / "restic").mkdir()
            (root / "restic" / "config").write_text("repo", encoding="utf-8")
            (root / "backup_verifier.db").write_text("keep", encoding="utf-8")
            n = wipe_local_dir_contents(root, data_dir=data)
            self.assertGreaterEqual(n, 2)
            self.assertTrue(root.is_dir())
            self.assertFalse((root / "paperless").exists())
            self.assertFalse((root / "restic").exists())
            self.assertEqual(
                (root / "backup_verifier.db").read_text(encoding="utf-8"), "keep"
            )


if __name__ == "__main__":
    unittest.main()
