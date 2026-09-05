"""Unit tests for fill projection, downsample, and status chips."""

from __future__ import annotations

import unittest

from app.core.storage_health import (
    chip_level_from_pct,
    downsample_samples,
    fill_projection,
    smart_chip,
    zfs_chip,
)


class ProjectionTests(unittest.TestCase):
    def test_two_samples_increasing(self) -> None:
        samples = [
            {"ts": 1_000_000, "used": 1000, "total": 10_000},
            {"ts": 1_000_000 + 2 * 86400, "used": 3000, "total": 10_000},
        ]
        # +2000 bytes / 2 days = 1000/day; remaining 7000 → 7 days
        out = fill_projection(samples)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["days"], 7)
        self.assertIn("7d", out["label"])

    def test_omit_when_not_enough_data(self) -> None:
        self.assertIsNone(fill_projection([{"ts": 1, "used": 10, "total": 100}]))
        self.assertIsNone(fill_projection([]))
        self.assertIsNone(
            fill_projection(
                [
                    {"ts": 1, "used": 50, "total": 100},
                    {"ts": 10, "used": 40, "total": 100},
                ]
            )
        )

    def test_used_plus_rate(self) -> None:
        out = fill_projection([], used=80, total=100, rate_per_day=10)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["days"], 2)

    def test_already_full(self) -> None:
        out = fill_projection([], used=100, total=100, rate_per_day=5)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["days"], 0)


class DownsampleTests(unittest.TestCase):
    def test_hourly_collapse_and_cap(self) -> None:
        now = 2_000_000.0
        samples = []
        for i in range(80):
            samples.append({"ts": now - i * 1800, "used": 10 + i, "total": 100})
        out = downsample_samples(samples, now_epoch=now, max_hourly=10, max_daily=5)
        self.assertLessEqual(len(out), 15)
        stamps = [r["ts"] for r in out]
        self.assertEqual(stamps, sorted(stamps))

    def test_daily_bucket_keeps_latest(self) -> None:
        # Older than hourly window (10h) → daily bucket
        now = 1_000_000.0
        samples = [
            {"ts": now - 40 * 3600, "used": 1, "total": 10},
            {"ts": now - 39 * 3600, "used": 9, "total": 10},
        ]
        out = downsample_samples(samples, now_epoch=now, max_hourly=10, max_daily=10)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["used"], 9)


class ChipTests(unittest.TestCase):
    def test_pct_levels(self) -> None:
        self.assertEqual(chip_level_from_pct(12), "ok")
        self.assertEqual(chip_level_from_pct(75), "warn")
        self.assertEqual(chip_level_from_pct(91), "danger")
        self.assertEqual(chip_level_from_pct(None), "unknown")

    def test_smart_and_zfs(self) -> None:
        self.assertEqual(smart_chip("PASSED"), "ok")
        self.assertEqual(smart_chip("OK", prefail=True), "warn")
        self.assertEqual(smart_chip("FAILED"), "danger")
        self.assertEqual(zfs_chip("ONLINE"), "ok")
        self.assertEqual(zfs_chip("DEGRADED"), "warn")
        self.assertEqual(zfs_chip("FAULTED"), "danger")


if __name__ == "__main__":
    unittest.main()
