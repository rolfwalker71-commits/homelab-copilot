"""Unit tests for SI byte formatting, percent clamp, and df/statvfs parse."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_MODULES = Path(__file__).resolve().parents[2] / "modules"
if _MODULES.is_dir() and str(_MODULES) not in sys.path:
    sys.path.insert(0, str(_MODULES))

from app.core.backup_storage import (
    bagel_dasharray,
    clamp_percent,
    format_si_de,
    parse_df_output,
    pick_hetzner_dest,
    serialize_usage,
    usage_from_vfs,
)


class FormatSiDeTests(unittest.TestCase):
    def test_bytes_and_german_comma(self) -> None:
        self.assertEqual(format_si_de(0), "0 B")
        self.assertEqual(format_si_de(500), "500 B")
        self.assertEqual(format_si_de(1200), "1,2 KB")
        self.assertEqual(format_si_de(12.4 * 1_000_000_000), "12,4 GB")
        self.assertEqual(format_si_de(80_000_000_000), "80,0 GB")

    def test_invalid_is_dash(self) -> None:
        self.assertEqual(format_si_de(None), "—")
        self.assertEqual(format_si_de("x"), "—")
        self.assertEqual(format_si_de(-20), "0 B")


class ClampPercentTests(unittest.TestCase):
    def test_normal_and_edges(self) -> None:
        self.assertEqual(clamp_percent(12, 100), 12.0)
        self.assertEqual(clamp_percent(90, 100), 90.0)
        self.assertEqual(clamp_percent(0, 80), 0.0)

    def test_clamp_and_unknown(self) -> None:
        self.assertEqual(clamp_percent(150, 100), 100.0)
        self.assertEqual(clamp_percent(-5, 100), 0.0)
        self.assertIsNone(clamp_percent(10, 0))
        self.assertIsNone(clamp_percent(None, 100))
        self.assertIsNone(clamp_percent(10, None))


class UsageFromVfsTests(unittest.TestCase):
    def test_used_is_total_minus_free(self) -> None:
        used, free, total = usage_from_vfs(frsize=4096, blocks=1000, bavail=400, bfree=450)
        self.assertEqual(total, 4096 * 1000)
        self.assertEqual(free, 4096 * 400)
        self.assertEqual(used, total - free)

    def test_reject_empty(self) -> None:
        self.assertIsNone(usage_from_vfs(frsize=0, blocks=10, bavail=1))
        self.assertIsNone(usage_from_vfs(frsize=4096, blocks=0, bavail=0))


class ParseDfTests(unittest.TestCase):
    def test_standard_1k_blocks(self) -> None:
        text = (
            "Filesystem     1K-blocks    Used Available Use% Mounted on\n"
            "/dev/sda1       83886080 12582912  71235584  15% /home\n"
        )
        parsed = parse_df_output(text, block_bytes=1024)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        used, free, total = parsed
        self.assertEqual(total, 83886080 * 1024)
        self.assertEqual(free, 71235584 * 1024)
        self.assertEqual(used, total - free)

    def test_wrapped_storage_box_line(self) -> None:
        text = (
            "Filesystem           1K-blocks      Used Available Use% Mounted on\n"
            "u12345.your-storagebox.de:/home\n"
            "                      52428800   8388608  44040192  16% /home\n"
        )
        parsed = parse_df_output(text)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        _used, free, total = parsed
        self.assertEqual(total, 52428800 * 1024)
        self.assertEqual(free, 44040192 * 1024)

    def test_garbage_is_none(self) -> None:
        self.assertIsNone(parse_df_output(""))
        self.assertIsNone(parse_df_output("Filesystem\nnone"))
        self.assertIsNone(parse_df_output("df: not supported"))


class SerializeAndBagelTests(unittest.TestCase):
    def test_warn_at_ninety(self) -> None:
        row = serialize_usage(90, 10, 100, source="statvfs")
        self.assertTrue(row["warn"])
        self.assertEqual(row["used_pct"], 90.0)
        self.assertEqual(row["used_label"], "90 B")

    def test_bagel_dash_clamps(self) -> None:
        self.assertEqual(bagel_dasharray(0, circumference=100), "0.00 100.00")
        self.assertEqual(bagel_dasharray(50, circumference=100), "50.00 50.00")
        self.assertEqual(bagel_dasharray(150, circumference=100), "100.00 0.00")
        self.assertEqual(bagel_dasharray(None, circumference=100), "0.00 100.00")


class PickHetznerDestTests(unittest.TestCase):
    def test_picks_storage_box_preset(self) -> None:
        rows = [
            {"kind": "copilot", "enabled": True, "host": ""},
            {
                "kind": "sftp",
                "enabled": True,
                "preset": "storage_box",
                "host": "u1.your-storagebox.de",
                "remote_path": "/home",
            },
        ]
        dest = pick_hetzner_dest(rows)
        self.assertIsNotNone(dest)
        assert dest is not None
        self.assertEqual(dest["host"], "u1.your-storagebox.de")

    def test_ignores_synology_and_disabled(self) -> None:
        rows = [
            {
                "kind": "sftp",
                "enabled": True,
                "preset": "synology",
                "host": "nas.lan",
            },
            {
                "kind": "sftp",
                "enabled": False,
                "preset": "storage_box",
                "host": "u1.your-storagebox.de",
            },
        ]
        self.assertIsNone(pick_hetzner_dest(rows))
        self.assertIsNone(pick_hetzner_dest([]))


if __name__ == "__main__":
    unittest.main()
