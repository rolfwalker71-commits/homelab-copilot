"""Unit tests for hop-pinned do-release-upgrade command (no live SSH)."""

from __future__ import annotations

import unittest
from datetime import date

from patcher.distupgrade_quirks import (
    HLOPS_SKIP_MIGRATE_FLAG,
    apply_extracted_cache_patches,
    apply_extracted_controller_patches,
    apply_extracted_main_patches,
    classic_sources_snippet,
    patch_signed_by_section,
    remap_ubuntu_suite,
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

_PLUCKY_CONTROLLER = '''
class DistUpgradeController:
    def _addSecuritySources(self):
        e.section['Signed-By'] = '/usr/share/keyrings/ubuntu-archive-keyring.gpg'

    def migrateToDeb822Sources(self):
        logging.debug("migrateToDeb822Sources()")
        self._do_migrate()

    def updateDeb822Sources(self):
        """
        deb822-aware version of updateSourcesList()
        """
        logging.debug("updateDeb822Sources()")
        self.sources = SourcesList(matcherPath=self.datadir, deb822=True)
        self.sources.backup(self.sources_backup_ext)

        if not self.rewriteDeb822Sources():
            self.abort()

        # Ensure suites and components are sorted.
        for entry in self.sources:
            if entry.disabled or entry.invalid:
                continue

            entry.comps = sorted(entry.comps, key=component_ordering_key)
            entry.suites = sorted(entry.suites, key=suite_ordering_key)

        self.sources.save()

        return True

    def doUpdate(self, showErrors=True, forceRetries=None):
        logging.debug("running doUpdate() (showErrors=%s)" % showErrors)
        logging.error("doUpdate() failed completely")
        if showErrors:
            self._view.error("Error during update", "network")
        return False
'''

_PLUCKY_CACHE = '''
    if kernel == 0:
        logging.warning(
            "estimate_kernel_initrd_size_in_boot() returned '0' for kernel?")
        kernel = 16*1024*1024
    if initrd == 0:
        logging.warning(
            "estimate_kernel_initrd_size_in_boot() returned '0' for initrd?")
        initrd = 175*1024*1024
'''

_PLUCKY_MAIN = """
SYSTEM_DIRS = ["/bin",
              "/boot",
              "/etc",
              "/initrd",
              "/lib",
              "/usr",
              "/var",
              ]
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
        self.assertIn("HLOPS_FROM_SUITE=\"oracular\"", cmd)
        self.assertIn("HLOPS_TO_SUITE=\"plucky\"", cmd)
        self.assertIn("updateDeb822Sources() skipped by hlops", cmd)
        self.assertIn("SourceEntry.suites has no setter", cmd)
        self.assertIn("doUpdate() cache.update failed — hlops continues", cmd)
        self.assertIn("LXC has no /boot kernel", cmd)
        self.assertIn("hlops: LXC ohne Kernel in /boot", cmd)
        self.assertIn("apt-get update nach Quellen-Umschreibung", cmd)
        self.assertIn("old-releases.ubuntu.com/ubuntu", cmd)
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
        self.assertFalse(result.update_deb822_guard)
        self.assertIn(HLOPS_SKIP_MIGRATE_FLAG, result.text)
        self.assertIn("skipped by hlops", result.text)
        self.assertTrue(
            any("migrateToDeb822Sources übersprungen" in n for n in result.notes)
        )
        keep = apply_extracted_controller_patches(_LINE_761, skip_migrate=False)
        self.assertFalse(keep.migrate_guard)
        self.assertFalse(keep.update_deb822_guard)
        self.assertNotIn(HLOPS_SKIP_MIGRATE_FLAG, keep.text)

    def test_update_deb822_noop_and_suites_no_setter(self) -> None:
        result = apply_extracted_controller_patches(
            _PLUCKY_CONTROLLER, skip_migrate=True
        )
        self.assertEqual(result.signed_by_count, 1)
        self.assertTrue(result.migrate_guard)
        self.assertTrue(result.update_deb822_guard)
        self.assertGreaterEqual(result.suites_assign_count, 1)
        self.assertTrue(result.doupdate_guard)
        self.assertIn("updateDeb822Sources() skipped by hlops", result.text)
        self.assertIn("return True", result.text)
        self.assertIn("hlops: SourceEntry.suites has no setter", result.text)
        self.assertIn("try:", result.text)
        self.assertIn("except (AttributeError, TypeError):", result.text)
        self.assertIn("doUpdate() cache.update failed — hlops continues", result.text)
        self.assertTrue(
            any("updateDeb822Sources übersprungen" in n for n in result.notes)
        )
        self.assertTrue(
            any("entry.suites-Zuweisung abgesichert" in n for n in result.notes)
        )
        self.assertTrue(
            any("doUpdate() fährt bei cache.update-Fehler" in n for n in result.notes)
        )
        compile(result.text, "<plucky-controller>", "exec")
        again = apply_extracted_controller_patches(result.text, skip_migrate=True)
        self.assertFalse(again.migrate_guard)
        self.assertFalse(again.update_deb822_guard)
        self.assertEqual(again.suites_assign_count, 0)
        self.assertFalse(again.doupdate_guard)
        self.assertEqual(again.text, result.text)
        keep = apply_extracted_controller_patches(
            _PLUCKY_CONTROLLER, skip_migrate=False
        )
        self.assertFalse(keep.update_deb822_guard)
        self.assertFalse(keep.doupdate_guard)
        self.assertGreaterEqual(keep.suites_assign_count, 1)
        self.assertNotIn("updateDeb822Sources() skipped by hlops", keep.text)

    def test_multiline_suites_assign_not_split(self) -> None:
        src = (
            "    e.suites = sorted([self.toDist, self.toDist + '-updates'],\n"
            "                      key=suite_ordering_key)\n"
            "            entry.suites = sorted(entry.suites, key=suite_ordering_key)\n"
        )
        result = apply_extracted_controller_patches(src, skip_migrate=False)
        self.assertEqual(result.suites_assign_count, 1)
        self.assertIn("e.suites = sorted([self.toDist, self.toDist + '-updates'],", result.text)
        self.assertIn("key=suite_ordering_key)", result.text)
        self.assertIn("hlops: SourceEntry.suites has no setter", result.text)
        self.assertNotIn(
            "e.suites = sorted([self.toDist, self.toDist + '-updates'],\n"
            "    except",
            result.text,
        )

    def test_kernel_initrd_zero_and_system_dirs_boot(self) -> None:
        cache = apply_extracted_cache_patches(_PLUCKY_CACHE)
        self.assertTrue(cache.kernel_zero)
        self.assertIn("hlops: LXC has no /boot kernel", cache.text)
        self.assertIn("hlops: LXC has no /boot initrd", cache.text)
        self.assertNotIn("kernel = 16*1024*1024", cache.text)
        self.assertNotIn("initrd = 175*1024*1024", cache.text)
        self.assertTrue(
            any("Kernel/Initrd-Größe 0 bleibt 0" in n for n in cache.notes)
        )
        again = apply_extracted_cache_patches(cache.text)
        self.assertFalse(again.kernel_zero)
        self.assertEqual(again.text, cache.text)
        main = apply_extracted_main_patches(_PLUCKY_MAIN)
        self.assertTrue(main.boot_skip)
        self.assertIn("hlops: LXC ohne Kernel in /boot", main.text)
        self.assertIn("SYSTEM_DIRS = [d for d in SYSTEM_DIRS if d != '/boot']", main.text)
        self.assertTrue(any("/boot aus SYSTEM_DIRS" in n for n in main.notes))
        again_main = apply_extracted_main_patches(main.text)
        self.assertFalse(again_main.boot_skip)
        self.assertEqual(again_main.text, main.text)

    def test_remap_ubuntu_suite_and_classic_snippet(self) -> None:
        self.assertEqual(
            remap_ubuntu_suite("oracular-security", "oracular", "plucky"),
            "plucky-security",
        )
        self.assertEqual(remap_ubuntu_suite("oracular", "oracular", "plucky"), "plucky")
        self.assertEqual(
            remap_ubuntu_suite("noble-updates", "oracular", "plucky"),
            "noble-updates",
        )
        snippet = classic_sources_snippet(
            eol_codenames=("oracular", "plucky", "questing"),
            from_codename="oracular",
            to_codename="plucky",
        )
        self.assertIn("HLOPS_FROM_SUITE=\"oracular\"", snippet)
        self.assertIn("HLOPS_TO_SUITE=\"plucky\"", snippet)
        self.assertIn("remap_suite", snippet)
        self.assertIn("old-releases.ubuntu.com", snippet)
        self.assertIn("apt-get update nach Quellen-Umschreibung", snippet)
        self.assertIn("Suites umgeschrieben", snippet)

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
