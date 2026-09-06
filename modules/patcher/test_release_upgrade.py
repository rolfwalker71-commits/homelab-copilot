"""Unit tests for hop-pinned do-release-upgrade command (no live SSH)."""

from __future__ import annotations

import unittest
from datetime import date

from patcher.distupgrade_quirks import (
    HLOPS_SKIP_MIGRATE_FLAG,
    apply_extracted_controller_patches,
    patch_signed_by_section,
    should_skip_migrate_deb822,
)
from patcher.release import should_use_devel_flag, suggest_ubuntu_release
from patcher.release_upgrade import _release_upgrade_cmd, _set_prompt_cmd

ASOF = date(2026, 9, 5)

_LINE_761 = """
    def _addSecuritySources(self):
        for e in self.sources.list:
            e.section['Signed-By'] = '/usr/share/keyrings/ubuntu-archive-keyring.gpg'

    def migrateToDeb822Sources(self):
        logging.debug("migrateToDeb822Sources()")
        self._do_migrate()
"""


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
        self.assertIn("apt-clone", cmd)
        self.assertIn("python3-apt", cmd)
        self.assertIn("HLOPS_EOL_CODENAMES", cmd)
        self.assertIn("oracular", cmd)
        self.assertIn("klassisches sources.list-Format", cmd)
        self.assertIn("HLOPS_SKIP_MIGRATE=1", cmd)
        self.assertIn(HLOPS_SKIP_MIGRATE_FLAG, cmd)
        self.assertIn("hasattr", cmd)
        self.assertIn("signed_by", cmd)
        self.assertIn("_addSecuritySources gegen fehlendes .section", cmd)
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
        self.assertIn("HLOPS_SKIP_MIGRATE=1", cmd)
        self.assertIn("apt-clone", cmd)


class DistUpgradeQuirkTests(unittest.TestCase):
    def test_signed_by_section_guard_on_line_761_fixture(self) -> None:
        patched, n = patch_signed_by_section(_LINE_761)
        self.assertEqual(n, 1)
        self.assertIn("if hasattr(e, 'section'):", patched)
        self.assertIn("e.section['Signed-By'] = '/usr/share/keyrings/ubuntu-archive-keyring.gpg'", patched)
        self.assertIn("elif hasattr(e, 'signed_by'):", patched)
        self.assertIn("e.signed_by = '/usr/share/keyrings/ubuntu-archive-keyring.gpg'", patched)
        again, n2 = patch_signed_by_section(patched)
        self.assertEqual(n2, 0)
        self.assertEqual(again, patched)

    def test_migrate_skip_flag_injected(self) -> None:
        result = apply_extracted_controller_patches(_LINE_761, skip_migrate=True)
        self.assertEqual(result.signed_by_count, 1)
        self.assertTrue(result.migrate_guard)
        self.assertIn(HLOPS_SKIP_MIGRATE_FLAG, result.text)
        self.assertIn("skipped by hlops", result.text)
        self.assertTrue(
            any("migrateToDeb822Sources übersprungen" in n for n in result.notes)
        )
        keep = apply_extracted_controller_patches(_LINE_761, skip_migrate=False)
        self.assertFalse(keep.migrate_guard)
        self.assertNotIn(HLOPS_SKIP_MIGRATE_FLAG, keep.text)

    def test_skip_migrate_flag_lxc_and_eol(self) -> None:
        hop = _hop_2410_to_2504()
        self.assertTrue(should_skip_migrate_deb822(hop, container=True, today=ASOF))
        self.assertTrue(should_skip_migrate_deb822(hop, container=False, today=ASOF))
        lts = _hop_2404_to_2604()
        self.assertTrue(should_skip_migrate_deb822(lts, container=True, today=ASOF))
        self.assertFalse(should_skip_migrate_deb822(lts, container=False, today=ASOF))
        lts_cmd = _release_upgrade_cmd(lts, container=False)
        self.assertIn("HLOPS_SKIP_MIGRATE=0", lts_cmd)
        self.assertIn("hasattr", lts_cmd)


if __name__ == "__main__":
    unittest.main()
