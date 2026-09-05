"""Unit tests for Ubuntu/Debian release-upgrade mapping (no live SSH)."""

from __future__ import annotations

import unittest
from datetime import date

from patcher.release import (
    next_ubuntu_lts,
    next_ubuntu_series,
    sequential_hops,
    suggest_debian_release,
    suggest_ubuntu_release,
    ubuntu_series_is_eol,
    ubuntu_series_is_supported,
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
        self.assertEqual(s.hops[-1].target, "26.04")
        self.assertEqual(s.method, "do-release-upgrade")
        self.assertTrue(s.performable)

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
