"""Unit tests for restore-drill pass/fail and push gating."""

from __future__ import annotations

import unittest

from backup_verifier.drill import (
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    evaluate_restic_check,
    evaluate_tar_list,
    should_push_drill,
    summarize_drill_batch,
)


class ResticDrillTests(unittest.TestCase):
    def test_exit_zero_ok(self) -> None:
        out = evaluate_restic_check(exit_code=0, stdout="no errors were found")
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], STATUS_SUCCESS)

    def test_nonzero_fail(self) -> None:
        out = evaluate_restic_check(exit_code=1, stderr="pack abc missing")
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], STATUS_FAILED)
        self.assertIn("missing", out["detail"])


class TarDrillTests(unittest.TestCase):
    def test_listable_ok(self) -> None:
        out = evaluate_tar_list(readable=True, member_count=12)
        self.assertTrue(out["ok"])
        self.assertIn("12", out["detail"])

    def test_empty_or_unreadable_fail(self) -> None:
        self.assertEqual(evaluate_tar_list(readable=False)["status"], STATUS_FAILED)
        self.assertEqual(
            evaluate_tar_list(readable=True, member_count=0)["status"],
            STATUS_FAILED,
        )

    def test_never_require_full_download(self) -> None:
        out = evaluate_tar_list(readable=True, member_count=99, downloaded=True)
        self.assertEqual(out["status"], STATUS_SKIPPED)


class PushGateTests(unittest.TestCase):
    def test_fail_always_pushes(self) -> None:
        self.assertEqual(should_push_drill(None, STATUS_FAILED), "fail")
        self.assertEqual(should_push_drill(STATUS_FAILED, STATUS_FAILED), "fail")

    def test_first_success_after_fail(self) -> None:
        self.assertEqual(should_push_drill(STATUS_FAILED, STATUS_SUCCESS), "recovered")
        self.assertIsNone(should_push_drill(STATUS_SUCCESS, STATUS_SUCCESS))
        self.assertIsNone(should_push_drill(None, STATUS_SUCCESS))

    def test_batch_summary(self) -> None:
        batch = summarize_drill_batch(
            [
                {"status": STATUS_SUCCESS},
                {"status": STATUS_FAILED},
                {"status": STATUS_SKIPPED},
            ]
        )
        self.assertEqual(batch["status"], STATUS_FAILED)
        self.assertEqual(batch["fail_count"], 1)
        self.assertEqual(batch["ok_count"], 1)


if __name__ == "__main__":
    unittest.main()
