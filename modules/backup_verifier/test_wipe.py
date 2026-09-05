"""Unit tests for backup wipe keyword check and path jail."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backup_verifier.sshutil import assert_sftp_rm_target
from backup_verifier.wipe import (
    WipeError,
    assert_copilot_wipe_root,
    assert_safe_wipe_path,
    is_dest_account_root,
    normalize_wipe_path,
    public_sftp_wipe_target,
    validate_wipe_keyword,
    wipe_local_dir_contents,
)
from app.core.docker_control import DockerControlError


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

    def test_storage_box_home_contents_allowed(self) -> None:
        self.assertTrue(is_dest_account_root("/home"))
        self.assertTrue(is_dest_account_root("/home/"))
        self.assertFalse(is_dest_account_root("/"))
        self.assertFalse(is_dest_account_root("/var/backups/homelab-copilot"))
        out = assert_safe_wipe_path(
            "/home", allowed_root="/home", dest_contents=True
        )
        self.assertEqual(out, "/home")
        self.assertEqual(
            assert_safe_wipe_path("/home/", allowed_root="/home/", dest_contents=True),
            "/home",
        )
        self.assertEqual(
            assert_safe_wipe_path(
                "/home/restic", allowed_root="/home", dest_contents=True
            ),
            "/home/restic",
        )

    def test_guest_home_still_rejected(self) -> None:
        with self.assertRaises(WipeError) as ctx:
            assert_safe_wipe_path("/home", allowed_root="/home")
        self.assertIn("zu weit", ctx.exception.message)
        with self.assertRaises(WipeError):
            assert_safe_wipe_path("/home/", allowed_root="/home/", dest_contents=False)

    def test_dest_system_roots_still_rejected(self) -> None:
        for raw in ("/", "/etc", "/var", "/data", ".."):
            with self.subTest(raw=raw):
                with self.assertRaises(WipeError):
                    assert_safe_wipe_path(raw, allowed_root=raw, dest_contents=True)

    def test_public_sftp_home_is_safe(self) -> None:
        dest = {
            "id": 1,
            "label": "Box",
            "host": "u12345.your-storagebox.de",
            "preset": "storage_box",
            "remote_path": "/home",
        }
        pub = public_sftp_wipe_target(dest)
        self.assertTrue(pub["safe"])
        self.assertTrue(pub["contents_only"])
        self.assertTrue(pub["hetzner"])
        custom = public_sftp_wipe_target(
            {
                "id": 2,
                "label": "SFTP",
                "host": "nas.example",
                "preset": "custom",
                "remote_path": "/home/",
            }
        )
        self.assertTrue(custom["safe"])
        self.assertTrue(custom["contents_only"])
        self.assertFalse(custom["hetzner"])
        guest_like = public_sftp_wipe_target({**dest, "remote_path": "/"})
        self.assertFalse(guest_like["safe"])

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


class SftpRmJailTests(unittest.TestCase):
    def test_home_contents_only_allowed(self) -> None:
        self.assertEqual(
            assert_sftp_rm_target("/home", contents_only=True), "/home"
        )
        self.assertEqual(
            assert_sftp_rm_target("/home/", contents_only=True), "/home"
        )

    def test_home_without_contents_rejected(self) -> None:
        with self.assertRaises(DockerControlError) as ctx:
            assert_sftp_rm_target("/home", contents_only=False)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_system_roots_rejected(self) -> None:
        for raw in ("/", "/etc", "/var", "/data"):
            with self.subTest(raw=raw):
                with self.assertRaises(DockerControlError):
                    assert_sftp_rm_target(raw, contents_only=True)
                with self.assertRaises(DockerControlError):
                    assert_sftp_rm_target(raw, contents_only=False)

    def test_dotdot_rejected(self) -> None:
        with self.assertRaises(DockerControlError):
            assert_sftp_rm_target("/home/../etc", contents_only=True)


if __name__ == "__main__":
    unittest.main()
