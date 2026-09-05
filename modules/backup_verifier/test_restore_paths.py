"""Unit tests for restore path jail, dest modes, and typed confirm."""

from __future__ import annotations

import unittest
from pathlib import Path

from backup_verifier.restore_paths import (
    DEST_ORIGINAL,
    DEST_STAGING,
    PLACE_COPILOT,
    PLACE_GUEST,
    RestorePlanError,
    copilot_staging_dir,
    describe_restore,
    guest_staging_dir,
    infer_stack_from_browse_path,
    jail_member_path,
    jail_restore_paths,
    normalize_dest_mode,
    normalize_dest_place,
    validate_restore_confirm,
    validate_typed_confirm,
)


class DestModeTests(unittest.TestCase):
    def test_default_staging(self) -> None:
        self.assertEqual(normalize_dest_mode(None), DEST_STAGING)
        self.assertEqual(normalize_dest_mode("nach_staging"), DEST_STAGING)

    def test_original_aliases(self) -> None:
        self.assertEqual(normalize_dest_mode("originalpfad"), DEST_ORIGINAL)

    def test_reject_unknown(self) -> None:
        with self.assertRaises(RestorePlanError):
            normalize_dest_mode("overwrite")

    def test_original_forces_guest_place(self) -> None:
        self.assertEqual(
            normalize_dest_place("copilot", dest_mode=DEST_ORIGINAL),
            PLACE_GUEST,
        )
        self.assertEqual(
            normalize_dest_place("guest", dest_mode=DEST_STAGING),
            PLACE_GUEST,
        )
        self.assertEqual(
            normalize_dest_place(None, dest_mode=DEST_STAGING),
            PLACE_COPILOT,
        )


class PathJailTests(unittest.TestCase):
    def test_strips_absolute_and_dot(self) -> None:
        self.assertEqual(jail_member_path("/data/paperless/foo"), "data/paperless/foo")
        self.assertEqual(jail_member_path("./compose/docker-compose.yml"), "compose/docker-compose.yml")

    def test_rejects_dotdot(self) -> None:
        with self.assertRaises(RestorePlanError):
            jail_member_path("../etc/passwd")
        with self.assertRaises(RestorePlanError):
            jail_member_path("binds/../../etc/shadow")

    def test_rejects_empty(self) -> None:
        with self.assertRaises(RestorePlanError):
            jail_member_path("")
        with self.assertRaises(RestorePlanError):
            jail_member_path("/")

    def test_stack_scope_allows_empty_paths(self) -> None:
        self.assertEqual(jail_restore_paths([], scope="stack"), [])

    def test_paths_scope_requires_members(self) -> None:
        with self.assertRaises(RestorePlanError):
            jail_restore_paths([], scope="paths")
        self.assertEqual(
            jail_restore_paths(["binds/data", "binds/data"], scope="paths"),
            ["binds/data"],
        )


class TypedConfirmTests(unittest.TestCase):
    def test_staging_needs_no_typed(self) -> None:
        validate_typed_confirm("", dest_mode=DEST_STAGING, stack="paperless")
        validate_restore_confirm(
            confirm=True, dest_mode=DEST_STAGING, typed_confirm=None, stack="paperless"
        )

    def test_original_requires_stack_or_restore(self) -> None:
        validate_typed_confirm("RESTORE", dest_mode=DEST_ORIGINAL, stack="paperless")
        validate_typed_confirm("paperless", dest_mode=DEST_ORIGINAL, stack="paperless")
        with self.assertRaises(RestorePlanError):
            validate_typed_confirm("yes", dest_mode=DEST_ORIGINAL, stack="paperless")
        with self.assertRaises(RestorePlanError):
            validate_restore_confirm(
                confirm=True,
                dest_mode=DEST_ORIGINAL,
                typed_confirm="",
                stack="paperless",
            )

    def test_confirm_flag_required(self) -> None:
        with self.assertRaises(RestorePlanError):
            validate_restore_confirm(
                confirm=False,
                dest_mode=DEST_STAGING,
                typed_confirm=None,
                stack="x",
            )


class StagingDirTests(unittest.TestCase):
    def test_guest_and_copilot_dirs(self) -> None:
        self.assertEqual(
            guest_staging_dir("/var/backups/homelab-copilot", "paperless", "20260101-010203"),
            "/var/backups/homelab-copilot/restore/paperless/20260101-010203",
        )
        p = copilot_staging_dir(Path("/data/backups"), "my stack", "stamp")
        self.assertEqual(p, Path("/data/backups/_restore/my_stack/stamp"))

    def test_describe_original_warns(self) -> None:
        info = describe_restore(
            stack="paperless",
            source_label="Copilot",
            snapshot_or_archive="a.tar.gz",
            dest_mode="original",
            dest_place="copilot",
            scope="stack",
            paths=[],
            staging_path="/tmp/x",
        )
        self.assertTrue(info["overwrite"])
        self.assertTrue(info["requires_typed_confirm"])
        self.assertIn("überschreib", info["warning"].lower())

    def test_infer_browse_paths(self) -> None:
        restic = infer_stack_from_browse_path("restic/lxc:pve:105/paperless")
        self.assertEqual(restic["kind"], "restic")
        self.assertEqual(restic["project"], "paperless")
        tar = infer_stack_from_browse_path("paperless/paperless-2026.tar.gz")
        self.assertEqual(tar["kind"], "tar")
        self.assertEqual(tar["project"], "paperless")


if __name__ == "__main__":
    unittest.main()
