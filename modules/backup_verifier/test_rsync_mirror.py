"""Unit tests for LXC↔Copilot and Copilot→dest rsync vs SFTP decision."""

from __future__ import annotations

import unittest

from backup_verifier.destinations import (
    decide_dest_mirror_transport,
    dest_requires_remote_rsync,
    dest_rsync_ssh_port,
    dest_sftp_port,
    is_hetzner_storagebox,
)
from backup_verifier.restic import decide_guest_mirror_transport
from backup_verifier.sshutil import _format_rsync_progress


class DecideGuestMirrorTransportTests(unittest.TestCase):
    def test_have_rsync_uses_rsync(self) -> None:
        self.assertEqual(
            decide_guest_mirror_transport(
                guest_has_rsync=True, allow_install=True, install_ok=None
            ),
            "rsync",
        )
        self.assertEqual(
            decide_guest_mirror_transport(
                guest_has_rsync=True, allow_install=False, install_ok=False
            ),
            "rsync",
        )

    def test_missing_install_allowed_tries_apt(self) -> None:
        self.assertEqual(
            decide_guest_mirror_transport(
                guest_has_rsync=False, allow_install=True, install_ok=None
            ),
            "try_install",
        )

    def test_install_ok_then_rsync(self) -> None:
        self.assertEqual(
            decide_guest_mirror_transport(
                guest_has_rsync=True, allow_install=True, install_ok=True
            ),
            "rsync",
        )
        self.assertEqual(
            decide_guest_mirror_transport(
                guest_has_rsync=False, allow_install=True, install_ok=True
            ),
            "rsync",
        )

    def test_install_failed_falls_back_to_sftp(self) -> None:
        self.assertEqual(
            decide_guest_mirror_transport(
                guest_has_rsync=False, allow_install=True, install_ok=False
            ),
            "sftp",
        )

    def test_install_disabled_uses_sftp(self) -> None:
        self.assertEqual(
            decide_guest_mirror_transport(
                guest_has_rsync=False, allow_install=False, install_ok=None
            ),
            "sftp",
        )


class DecideDestRsyncPortTests(unittest.TestCase):
    def test_storagebox_host_defaults_ssh_23(self) -> None:
        self.assertEqual(
            dest_rsync_ssh_port({"host": "u12345.your-storagebox.de"}),
            23,
        )
        self.assertEqual(
            dest_rsync_ssh_port({"host": "u12345.your-storagebox.de", "port": 22}),
            23,
        )
        self.assertEqual(
            dest_rsync_ssh_port({"preset": "storage_box", "host": "box.example"}),
            23,
        )
        self.assertTrue(is_hetzner_storagebox("u12345.your-storagebox.de"))
        self.assertFalse(dest_requires_remote_rsync({"host": "u12345.your-storagebox.de"}))

    def test_explicit_port_wins(self) -> None:
        self.assertEqual(
            dest_rsync_ssh_port({"host": "u12345.your-storagebox.de", "port": 23}),
            23,
        )
        self.assertEqual(
            dest_rsync_ssh_port({"host": "u12345.your-storagebox.de", "port": 2222}),
            2222,
        )
        self.assertEqual(
            dest_rsync_ssh_port({"host": "nas.local", "port": 2222}),
            2222,
        )
        self.assertEqual(dest_rsync_ssh_port({"host": "nas.local", "port": 22}), 22)

    def test_storagebox_sftp_fallback_stays_22(self) -> None:
        self.assertEqual(
            dest_sftp_port({"host": "u12345.your-storagebox.de", "port": 23}),
            22,
        )
        self.assertEqual(
            dest_sftp_port({"host": "u12345.your-storagebox.de", "port": 22}),
            22,
        )
        self.assertEqual(
            dest_sftp_port({"preset": "storage_box", "host": "box.example"}),
            22,
        )


class DecideDestMirrorTransportTests(unittest.TestCase):
    def test_prefer_rsync_when_local_ok(self) -> None:
        self.assertEqual(
            decide_dest_mirror_transport(local_has_rsync=True),
            "rsync",
        )

    def test_fallback_sftp_when_no_local_rsync(self) -> None:
        self.assertEqual(
            decide_dest_mirror_transport(local_has_rsync=False),
            "sftp",
        )

    def test_fallback_sftp_when_rsync_failed(self) -> None:
        self.assertEqual(
            decide_dest_mirror_transport(local_has_rsync=True, rsync_ok=False),
            "sftp",
        )


class RsyncProgressFormatTests(unittest.TestCase):
    def test_progress_line_is_prefixed(self) -> None:
        msg = _format_rsync_progress(
            "  12.5M  40%  8.10MB/s    0:00:03 (xfr#4, to-chk=10/20)",
            label="Copilot",
        )
        self.assertTrue(msg.startswith("rsync → Copilot:"))
        self.assertIn("40%", msg)

    def test_blank_is_empty(self) -> None:
        self.assertEqual(_format_rsync_progress("   ", label="Host"), "")


if __name__ == "__main__":
    unittest.main()
