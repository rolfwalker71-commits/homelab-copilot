"""Local lessons + scan notes — no USN, no LLM."""

from __future__ import annotations

import unittest

from ops_agent.lessons import (
    ERROR_DISK,
    ERROR_DPKG_LOCK,
    ERROR_RATE_LIMIT,
    ERROR_TIMEOUT,
    classify_error_class,
    packages_key,
    scan_apply_note,
    should_hold,
)


class ClassifyTests(unittest.TestCase):
    def test_disk_and_lock_and_timeout(self) -> None:
        self.assertEqual(classify_error_class("ENOSPC disk voll"), ERROR_DISK)
        self.assertEqual(classify_error_class("Could not get lock /var/lib/dpkg"), ERROR_DPKG_LOCK)
        self.assertEqual(classify_error_class("SSH-Befehl-Timeout"), ERROR_TIMEOUT)
        self.assertEqual(
            classify_error_class(
                "Error toomanyrequests: You have reached your unauthenticated pull rate limit."
            ),
            ERROR_RATE_LIMIT,
        )


class HoldTests(unittest.TestCase):
    def test_one_timeout_does_not_hold(self) -> None:
        lessons = [
            {
                "packages_key": "curl",
                "host_kind": "lxc",
                "job_kind": "patch",
                "error_class": ERROR_TIMEOUT,
                "rollback_ran": False,
                "why_de": "Timeout.",
            }
        ]
        self.assertIsNone(
            should_hold(lessons, packages_key="curl", host_kind="lxc", job_kind="patch")
        )

    def test_rollback_holds_once(self) -> None:
        lessons = [
            {
                "id": 4,
                "packages_key": "curl",
                "host_kind": "lxc",
                "job_kind": "patch",
                "error_class": "apply_failed",
                "rollback_ran": True,
                "why_de": "Apply fehlgeschlagen.",
            }
        ]
        hold = should_hold(lessons, packages_key="curl", host_kind="lxc", job_kind="patch")
        self.assertIsNotNone(hold)
        assert hold is not None
        self.assertIn("Lektion", hold.reason)
        self.assertIn("durch Agent", hold.reason)

    def test_two_timeouts_hold(self) -> None:
        row = {
            "packages_key": "curl",
            "host_kind": "lxc",
            "job_kind": "patch",
            "error_class": ERROR_TIMEOUT,
            "rollback_ran": False,
            "why_de": "Timeout.",
        }
        hold = should_hold(
            [row, dict(row)], packages_key="curl", host_kind="lxc", job_kind="patch"
        )
        self.assertIsNotNone(hold)

    def test_rate_limit_never_holds_image(self) -> None:
        row = {
            "packages_key": "s1t5/mailarchiver:latest",
            "host_kind": "lxc",
            "job_kind": "image",
            "error_class": ERROR_RATE_LIMIT,
            "rollback_ran": False,
            "why_de": "Docker-Hub-Limit.",
        }
        self.assertIsNone(
            should_hold(
                [row, dict(row), dict(row)],
                packages_key="s1t5/mailarchiver:latest",
                host_kind="lxc",
                job_kind="image",
            )
        )


class ScanNoteTests(unittest.TestCase):
    def test_no_changelog_means_only_reboot_flag(self) -> None:
        self.assertIsNone(
            scan_apply_note(job_packages=["curl"], reboot_required=False, scan_packages=[])
        )
        self.assertEqual(
            scan_apply_note(job_packages=["curl"], reboot_required=True),
            "Reboot nötig",
        )

    def test_breaks_only_for_job_packages(self) -> None:
        note = scan_apply_note(
            job_packages=["curl"],
            reboot_required=False,
            scan_packages=[
                {"name": "curl", "meta": {"breaks": "wget"}},
                {"name": "other", "changelog": "USN-999"},
            ],
        )
        self.assertEqual(note, "Breaks wget")

    def test_packages_key(self) -> None:
        self.assertEqual(packages_key(["B", "a"]), "a,b")
        self.assertEqual(packages_key([], bucket="security"), "bucket:security")


if __name__ == "__main__":
    unittest.main()
