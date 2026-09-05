"""Unit tests for Ubuntu/Debian release-upgrade mapping (no live SSH)."""

from __future__ import annotations

import unittest
from datetime import date

from patcher.release import (
    build_meta_release_pin,
    detect_fetched_upgrade_codename,
    hop_failure_message,
    next_ubuntu_lts,
    next_ubuntu_series,
    sequential_hops,
    should_use_devel_flag,
    suggest_debian_release,
    suggest_ubuntu_release,
    ubuntu_series_is_eol,
    ubuntu_series_is_supported,
    ubuntu_upgrade_tool_mirror,
    upgrade_tool_url_candidates,
    upgrader_log_tail,
)

ASOF = date(2026, 9, 5)


class UbuntuSeriesTests(unittest.TestCase):
    def test_sequential_next(self) -> None:
        self.assertEqual(next_ubuntu_series("24.10"), "25.04")
        self.assertEqual(next_ubuntu_series("25.04"), "25.10")
        self.assertEqual(next_ubuntu_series("25.10"), "26.04")
        self.assertEqual(next_ubuntu_series("24.04"), "24.10")

    def test_eol_as_of_2026_09(self) -> None:
        self.assertTrue(ubuntu_series_is_eol("24.10", ASOF))
        self.assertTrue(ubuntu_series_is_eol("25.04", ASOF))
        self.assertTrue(ubuntu_series_is_eol("25.10", ASOF))
        self.assertFalse(ubuntu_series_is_eol("24.04", ASOF))
        self.assertFalse(ubuntu_series_is_eol("26.04", ASOF))
        self.assertTrue(ubuntu_series_is_supported("24.04", ASOF))
        self.assertTrue(ubuntu_series_is_supported("26.04", ASOF))
        self.assertFalse(ubuntu_series_is_supported("24.10", ASOF))

    def test_next_lts_does_not_skip_lts(self) -> None:
        self.assertEqual(next_ubuntu_lts("22.04", ASOF), "24.04")
        self.assertEqual(next_ubuntu_lts("24.04", ASOF), "26.04")
        self.assertEqual(next_ubuntu_lts("24.10", ASOF), "26.04")
        self.assertIsNone(next_ubuntu_lts("26.04", ASOF))

    def test_hops_2410_to_2604(self) -> None:
        self.assertEqual(
            sequential_hops("24.10", "26.04"),
            [("24.10", "25.04"), ("25.04", "25.10"), ("25.10", "26.04")],
        )


class UbuntuRecommendTests(unittest.TestCase):
    def test_2410_recommends_2604_not_eol_hop(self) -> None:
        s = suggest_ubuntu_release(
            version_id="24.10",
            pretty_name="Ubuntu 24.10",
            codename="oracular",
            today=ASOF,
        )
        self.assertIsNotNone(s)
        assert s is not None
        self.assertEqual(s.urgency, "recommended")
        self.assertEqual(s.target_version, "26.04")
        self.assertTrue(s.target_is_lts)
        self.assertTrue(s.current_eol)
        self.assertEqual(s.chip, "Release-Upgrade empfohlen")
        self.assertIn("24.10", s.headline)
        self.assertIn("26.04", s.headline)
        self.assertNotIn("25.04", s.headline)
        self.assertEqual(len(s.hops), 3)
        self.assertEqual(s.hops[0].source, "24.10")
        self.assertEqual(s.hops[0].target, "25.04")
        self.assertEqual(s.hops[0].target_codename, "plucky")
        self.assertEqual(s.hops[0].prompt, "normal")
        self.assertFalse(should_use_devel_flag(s.hops[0], ASOF))
        self.assertEqual(s.hops[1].target, "25.10")
        self.assertEqual(s.hops[1].target_codename, "questing")
        self.assertEqual(s.hops[1].prompt, "normal")
        self.assertEqual(s.hops[-1].target, "26.04")
        self.assertEqual(s.hops[-1].target_codename, "resolute")
        self.assertEqual(s.hops[-1].prompt, "normal")
        self.assertEqual(s.method, "do-release-upgrade")
        self.assertTrue(s.performable)
        for hop in s.hops:
            self.assertFalse(should_use_devel_flag(hop, ASOF))

    def test_2504_and_2510_also_to_2604(self) -> None:
        a = suggest_ubuntu_release(version_id="25.04", today=ASOF)
        b = suggest_ubuntu_release(version_id="25.10", today=ASOF)
        self.assertEqual(a and a.target_version, "26.04")
        self.assertEqual(b and b.target_version, "26.04")
        self.assertEqual(len(a.hops), 2)  # type: ignore[union-attr]
        self.assertEqual(len(b.hops), 1)  # type: ignore[union-attr]
        self.assertEqual(a.urgency, "recommended")  # type: ignore[union-attr]
        self.assertEqual(b.urgency, "recommended")  # type: ignore[union-attr]

    def test_2404_optional_next_lts_not_eol_copy(self) -> None:
        s = suggest_ubuntu_release(
            version_id="24.04",
            pretty_name="Ubuntu 24.04.3 LTS",
            today=ASOF,
        )
        self.assertIsNotNone(s)
        assert s is not None
        self.assertEqual(s.urgency, "optional")
        self.assertEqual(s.target_version, "26.04")
        self.assertFalse(s.current_eol)
        self.assertEqual(s.chip, "Nächstes LTS verfügbar")
        self.assertIn("26.04", s.headline)
        self.assertNotIn("End-of-Life", s.reason)
        self.assertNotIn("am Ende", s.reason)
        self.assertEqual(len(s.hops), 1)
        self.assertEqual(s.hops[0].prompt, "lts")
        self.assertFalse(should_use_devel_flag(s.hops[0], ASOF))

    def test_2604_no_nag(self) -> None:
        self.assertIsNone(suggest_ubuntu_release(version_id="26.04", today=ASOF))

    def test_2204_next_lts_is_2404(self) -> None:
        s = suggest_ubuntu_release(version_id="22.04", today=ASOF)
        self.assertIsNotNone(s)
        assert s is not None
        self.assertEqual(s.target_version, "24.04")
        self.assertEqual(s.urgency, "optional")
        self.assertEqual(len(s.hops), 1)

    def test_supported_interim_prefers_next_release_if_lts_not_out(self) -> None:
        # 2025-05-01: 24.10 still supported, 25.04 out, 26.04 not yet.
        today = date(2025, 5, 1)
        self.assertFalse(ubuntu_series_is_eol("24.10", today))
        s = suggest_ubuntu_release(version_id="24.10", today=today)
        self.assertIsNotNone(s)
        assert s is not None
        self.assertEqual(s.target_version, "25.04")
        self.assertEqual(s.urgency, "optional")
        self.assertFalse(s.current_eol)

    def test_eol_interim_prefers_next_supported_if_lts_not_out(self) -> None:
        # 2025-08-01: 24.10 EOL, 25.04 still supported, 26.04 not released.
        today = date(2025, 8, 1)
        s = suggest_ubuntu_release(version_id="24.10", today=today)
        self.assertIsNotNone(s)
        assert s is not None
        self.assertEqual(s.target_version, "25.04")
        self.assertEqual(s.urgency, "recommended")
        self.assertTrue(s.current_eol)


class HopPinTests(unittest.TestCase):
    def test_2410_pin_is_plucky_not_resolute(self) -> None:
        pin = build_meta_release_pin(source="24.10", target="25.04", today=ASOF)
        self.assertIn("Dist: oracular", pin)
        self.assertIn("Dist: plucky", pin)
        self.assertIn("Supported: 1", pin)
        self.assertIn("plucky.tar.gz", pin)
        self.assertNotIn("resolute", pin)
        self.assertNotIn("questing", pin)
        self.assertIn("old-releases.ubuntu.com", pin)
        plucky_block = pin.split("Dist: plucky", 1)[1]
        self.assertIn("old-releases.ubuntu.com", plucky_block)
        self.assertIn(
            "http://archive.ubuntu.com/ubuntu/dists/plucky/main/"
            "dist-upgrader-all/current/plucky.tar.gz",
            plucky_block,
        )
        self.assertNotIn("plucky-updates", plucky_block)

    def test_eol_tool_urls_use_dists_codename_not_updates(self) -> None:
        self.assertEqual(
            ubuntu_upgrade_tool_mirror("25.04", ASOF),
            "http://old-releases.ubuntu.com/ubuntu",
        )
        self.assertEqual(
            ubuntu_upgrade_tool_mirror("25.10", ASOF),
            "http://old-releases.ubuntu.com/ubuntu",
        )
        self.assertEqual(
            ubuntu_upgrade_tool_mirror("26.04", ASOF),
            "http://archive.ubuntu.com/ubuntu",
        )
        # HEAD 2026-09-05: plucky.tar.gz is on archive, not old-releases.
        plucky = upgrade_tool_url_candidates("25.04", ASOF)
        self.assertEqual(
            plucky[0],
            "http://archive.ubuntu.com/ubuntu/dists/plucky/main/"
            "dist-upgrader-all/current/plucky.tar.gz",
        )
        self.assertIn(
            "http://old-releases.ubuntu.com/ubuntu/dists/plucky/main/"
            "dist-upgrader-all/current/plucky.tar.gz",
            plucky,
        )
        self.assertTrue(any("archive.ubuntu.com" in u for u in plucky))
        for version in ("25.04", "25.10", "26.04"):
            urls = upgrade_tool_url_candidates(version, ASOF)
            self.assertTrue(urls)
            for url in urls:
                self.assertNotRegex(url, r"dists/[a-z]+-updates/")
                self.assertNotRegex(url, r"dists/[a-z]+-security/")
                self.assertRegex(
                    url,
                    r"/dists/[a-z]+(?:-proposed)?/main/dist-upgrader-all/current/[a-z]+\.tar\.gz$",
                )
        questing = upgrade_tool_url_candidates("25.10", ASOF)
        self.assertEqual(
            questing[0],
            "http://archive.ubuntu.com/ubuntu/dists/questing/main/"
            "dist-upgrader-all/current/questing.tar.gz",
        )
        resolute = upgrade_tool_url_candidates("26.04", ASOF)
        self.assertEqual(
            resolute[0],
            "http://archive.ubuntu.com/ubuntu/dists/resolute/main/"
            "dist-upgrader-all/current/resolute.tar.gz",
        )

    def test_failure_mentions_leaked_resolute_and_snap(self) -> None:
        s = suggest_ubuntu_release(version_id="24.10", today=ASOF)
        assert s is not None
        hop = s.hops[0]
        blob = (
            "/usr/lib/python3/dist-packages/DistUpgrade/"
            "DistUpgradeFetcherCore.py:237: Warning: W:Download is performed "
            "unsandboxed as root as file 'resolute.tar.gz.gpg' couldn't be "
            "accessed by user '_apt'."
        )
        msg = hop_failure_message(hop, 1, blob)
        self.assertIn("24.10 → 25.04", msg)
        self.assertIn("resolute.tar.gz", msg)
        self.assertIn("26.04", msg)
        self.assertIn("plucky", msg)
        self.assertIn("hlops-", msg)
        self.assertEqual(detect_fetched_upgrade_codename(blob), "resolute")

    def test_failure_shows_last_30_lines_not_prefix(self) -> None:
        s = suggest_ubuntu_release(version_id="24.10", today=ASOF)
        assert s is not None
        hop = s.hops[0]
        prefix = "\n".join(
            f"Reading package lists... line {i}" for i in range(40)
        )
        prefix += "\nDistUpgrade-Tarball gefunden: http://archive.ubuntu.com/ubuntu/dists/plucky/..."
        suffix = (
            "----- letzte 30 Zeilen /var/log/dist-upgrade/main.log -----\n"
            "ERROR Can not write to '/boot'\n"
            "Its not possible to write to the system directory '/boot'\n"
            "DistUpgrade Exit: 1"
        )
        blob = prefix + "\n" + suffix
        msg = hop_failure_message(hop, 1, blob)
        self.assertIn("24.10 → 25.04", msg)
        self.assertIn("Can not write to '/boot'", msg)
        self.assertIn("DistUpgrade Exit: 1", msg)
        self.assertNotIn("Reading package lists... line 0", msg)
        self.assertNotIn("Reading package lists... line 9", msg)
        tail = upgrader_log_tail(blob, 30)
        self.assertIn("Can not write to '/boot'", tail)
        self.assertNotIn("line 0", tail)


class DebianSuggestTests(unittest.TestCase):
    def test_bookworm_trixie_suggest_only(self) -> None:
        s = suggest_debian_release(
            version_id="12",
            pretty_name="Debian GNU/Linux 12 (bookworm)",
            codename="bookworm",
        )
        self.assertIsNotNone(s)
        assert s is not None
        self.assertEqual(s.family, "debian")
        self.assertEqual(s.target_codename, "trixie")
        self.assertFalse(s.performable)
        self.assertEqual(s.method, "debian-suggest")

    def test_trixie_no_nag(self) -> None:
        self.assertIsNone(
            suggest_debian_release(version_id="13", codename="trixie")
        )


if __name__ == "__main__":
    unittest.main()
