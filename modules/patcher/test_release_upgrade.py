"""Unit tests for hop-pinned do-release-upgrade command (no live SSH)."""

from __future__ import annotations

import unittest
from datetime import date

from patcher.release import should_use_devel_flag, suggest_ubuntu_release
from patcher.release_upgrade import _release_upgrade_cmd, _set_prompt_cmd

ASOF = date(2026, 9, 5)


def _hop_2410_to_2504():
    s = suggest_ubuntu_release(version_id="24.10", today=ASOF)
    assert s is not None
    return s.hops[0]


def _hop_2404_to_2604():
    s = suggest_ubuntu_release(version_id="24.04", today=ASOF)
    assert s is not None
    return s.hops[0]


class ReleaseUpgradeCmdTests(unittest.TestCase):
    def test_2410_hop1_pins_plucky_no_devel_flag(self) -> None:
        hop = _hop_2410_to_2504()
        self.assertEqual(hop.target, "25.04")
        self.assertEqual(hop.target_codename, "plucky")
        self.assertEqual(hop.prompt, "normal")
        self.assertFalse(should_use_devel_flag(hop, ASOF))
        cmd = _release_upgrade_cmd(hop, container=True)
        self.assertIn("plucky", cmd)
        self.assertIn("plucky.tar.gz", cmd)
        self.assertIn(
            "http://archive.ubuntu.com/ubuntu/dists/plucky/main/"
            "dist-upgrader-all/current/plucky.tar.gz",
            cmd,
        )
        self.assertIn("old-releases.ubuntu.com", cmd)
        self.assertNotIn("plucky-updates", cmd)
        self.assertNotIn("dists/plucky-updates/", cmd)
        self.assertIn("/var/tmp/ubuntu-release-upgrader", cmd)
        self.assertIn('APT::Sandbox::User "root"', cmd)
        self.assertIn("file:///var/tmp/ubuntu-release-upgrader/meta-release", cmd)
        self.assertIn("Dist: plucky", cmd)
        self.assertIn("Supported: 1", cmd)
        self.assertIn("Versuche DistUpgrade-Tarball:", cmd)
        self.assertIn("tar --no-same-owner", cmd)
        self.assertIn("$CODE.d", cmd)
        self.assertIn("--datadir=.", cmd)
        self.assertIn("/var/log/dist-upgrade/main.log", cmd)
        self.assertIn("tail -n 30", cmd)
        self.assertIn("release-upgrader-sshd.pid", cmd)
        self.assertIn("gpgv", cmd)
        self.assertIn("chmod a+x", cmd)
        self.assertNotIn("resolute.tar.gz", cmd)
        self.assertNotIn("do-release-upgrade -d", cmd)
        self.assertNotRegex(cmd, r"do-release-upgrade\s+-d\b")
        prompt = _set_prompt_cmd(hop.prompt)
        self.assertIn("Prompt=normal", prompt)
        self.assertNotIn("Prompt=lts", prompt)

    def test_lts_hop_uses_prompt_lts_still_no_devel_flag(self) -> None:
        hop = _hop_2404_to_2604()
        self.assertEqual(hop.prompt, "lts")
        self.assertFalse(should_use_devel_flag(hop, ASOF))
        cmd = _release_upgrade_cmd(hop, container=False)
        self.assertIn("resolute.tar.gz", cmd)
        self.assertIn("archive.ubuntu.com", cmd)
        self.assertIn(
            "http://archive.ubuntu.com/ubuntu/dists/resolute/main/"
            "dist-upgrader-all/current/resolute.tar.gz",
            cmd,
        )
        self.assertNotIn("resolute-updates", cmd)
        self.assertNotIn("do-release-upgrade -d", cmd)
        prompt = _set_prompt_cmd(hop.prompt)
        self.assertIn("Prompt=lts", prompt)

    def test_2510_hop_tool_is_old_releases(self) -> None:
        s = suggest_ubuntu_release(version_id="25.04", today=ASOF)
        assert s is not None
        hop = s.hops[0]
        self.assertEqual(hop.target, "25.10")
        cmd = _release_upgrade_cmd(hop, container=True)
        self.assertIn("questing.tar.gz", cmd)
        self.assertIn(
            "http://archive.ubuntu.com/ubuntu/dists/questing/main/"
            "dist-upgrader-all/current/questing.tar.gz",
            cmd,
        )
        self.assertIn("old-releases.ubuntu.com", cmd)
        self.assertNotIn("questing-updates", cmd)
        self.assertNotIn("do-release-upgrade -d", cmd)
        self.assertNotIn("resolute.tar.gz", cmd)


if __name__ == "__main__":
    unittest.main()
