"""Wellen-Agent: grouping, gates, stop-on-fail, explanations without LLM."""

from __future__ import annotations

import unittest

from patcher.agent import (
    AgentPolicy,
    HostContext,
    HostPending,
    PlannedItem,
    can_auto_apply_security,
    evaluate_gates,
    group_wave,
    mark_skipped_after_failure,
    next_wave_status,
    package_confirm_reason,
)
from patcher.explain import explain_apply_run, explain_patch_job, explain_wave_item


def _pkg(name: str, *, priority: str = "normal", archive: str | None = None) -> dict:
    meta = {"archive": archive} if archive else {}
    return {"name": name, "priority": priority, "meta": meta, "archive": archive}


class GroupingTests(unittest.TestCase):
    def test_security_vs_confirm(self) -> None:
        hosts = [
            HostPending(
                target_id="lxc:10",
                target_name="web",
                packages=[
                    _pkg("openssl", priority="security", archive="noble-security"),
                    _pkg("vim", priority="normal"),
                    _pkg("linux-image-generic", priority="security", archive="noble-security"),
                    _pkg("docker-ce", priority="security"),
                ],
            )
        ]
        items = group_wave(hosts)
        buckets = [i.bucket for i in items]
        self.assertEqual(buckets, ["security", "regular"])
        sec = items[0]
        self.assertFalse(sec.needs_confirm)
        self.assertEqual(sec.package_filter, "security")
        self.assertIn("openssl", sec.packages)
        self.assertNotIn("linux-image-generic", sec.packages)
        self.assertNotIn("vim", sec.packages)
        confirm = items[1]
        self.assertTrue(confirm.needs_confirm)
        self.assertIn("kernel", confirm.confirm_reasons)
        self.assertIn("docker", confirm.confirm_reasons)
        self.assertIn("regular", confirm.confirm_reasons)
        self.assertIn("linux-image-generic", confirm.packages)
        self.assertIn("docker-ce", confirm.packages)
        self.assertIn("vim", confirm.packages)

    def test_esm_counts_as_security(self) -> None:
        why = package_confirm_reason(
            _pkg("ca-certificates", priority="normal", archive="noble-esm-infra")
        )
        self.assertIsNone(why)

    def test_no_auto_patch_forces_confirm(self) -> None:
        hosts = [
            HostPending(
                target_id="lxc:11",
                target_name="prod",
                packages=[_pkg("openssl", priority="security", archive="security")],
                tags=["no-auto-patch"],
            )
        ]
        items = group_wave(hosts)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].needs_confirm)
        self.assertEqual(items[0].bucket, "regular")
        self.assertIn("no-auto-patch", items[0].confirm_reasons)

    def test_images_last(self) -> None:
        hosts = [
            HostPending(
                target_id="lxc:12",
                target_name="alpha",
                packages=[_pkg("curl", priority="normal")],
                image_updates=2,
                image_names=["app", "db"],
            ),
            HostPending(
                target_id="lxc:13",
                target_name="beta",
                packages=[_pkg("openssl", priority="security", archive="security")],
            ),
        ]
        items = group_wave(hosts)
        self.assertEqual([i.bucket for i in items], ["security", "regular", "images"])
        self.assertEqual(items[-1].packages, ["app", "db"])
        self.assertTrue(items[-1].needs_confirm)


class GateTests(unittest.TestCase):
    def test_gates_block_auto_apply(self) -> None:
        policy = AgentPolicy(enabled=True, auto_security=True, max_parallel=1)
        item = PlannedItem(
            target_id="lxc:1",
            target_name="web",
            bucket="security",
            needs_confirm=False,
            confirm_reasons=[],
            package_filter="security",
            packages=["openssl"],
            reason="sec",
        )
        self.assertTrue(can_auto_apply_security(item, policy=policy, gates=[]))
        offline = evaluate_gates(HostContext(target_id="lxc:1", online=False))
        self.assertTrue(offline)
        self.assertFalse(can_auto_apply_security(item, policy=policy, gates=offline))
        disk = evaluate_gates(HostContext(target_id="lxc:1", disk_pct=96.0))
        self.assertTrue(any("Disk" in g for g in disk))
        self.assertFalse(can_auto_apply_security(item, policy=policy, gates=disk))
        backup = evaluate_gates(HostContext(target_id="lxc:1", backup_running=True))
        self.assertTrue(any("Backup" in g for g in backup))
        self.assertFalse(can_auto_apply_security(item, policy=policy, gates=backup))

    def test_auto_off_by_default(self) -> None:
        item = PlannedItem(
            target_id="lxc:1",
            target_name="web",
            bucket="security",
            needs_confirm=False,
            confirm_reasons=[],
            package_filter="security",
            packages=["openssl"],
            reason="sec",
        )
        self.assertFalse(
            can_auto_apply_security(item, policy=AgentPolicy(), gates=[])
        )


class StopOnFailTests(unittest.TestCase):
    def test_stop_on_fail_skips_rest(self) -> None:
        items = [
            {"id": 1, "status": "success"},
            {"id": 2, "status": "running"},
            {"id": 3, "status": "ready"},
            {"id": 4, "status": "waiting_confirm"},
        ]
        out = mark_skipped_after_failure(items, failed_item_id=2)
        by_id = {i["id"]: i["status"] for i in out}
        self.assertEqual(by_id[1], "success")
        self.assertEqual(by_id[2], "failed")
        self.assertEqual(by_id[3], "skipped")
        self.assertEqual(by_id[4], "skipped")
        self.assertEqual(
            next_wave_status(item_ok=False, remaining_runnable=2, remaining_waiting=1),
            "failed",
        )
        self.assertEqual(
            next_wave_status(item_ok=True, remaining_runnable=0, remaining_waiting=2),
            "waiting",
        )
        self.assertEqual(
            next_wave_status(item_ok=True, remaining_runnable=0, remaining_waiting=0),
            "completed",
        )


class ExplanationTests(unittest.TestCase):
    def test_explanation_without_llm(self) -> None:
        text = explain_wave_item(
            {
                "target_name": "web",
                "bucket": "regular",
                "status": "waiting_confirm",
                "package_filter": "selected",
                "packages": ["linux-image-generic"],
                "confirm_reasons": ["kernel"],
                "gates": [],
            }
        )
        self.assertGreaterEqual(text.count("."), 2)
        self.assertIn("Kernel", text)
        self.assertIn("Bestätigung", text)
        self.assertNotIn("PATCHER_LLM_API_KEY", text)

        failed = explain_patch_job(
            {
                "kind": "apply",
                "status": "failed",
                "target_id": "lxc:10",
                "error": "apt-get exit 1",
                "message": "Fehlgeschlagen",
            }
        )
        self.assertIn("Fehlgeschlagen", failed)
        self.assertIn("Snapshot", failed)
        self.assertNotIn("sk-", failed)

        hist = explain_apply_run(
            {
                "target_name": "web",
                "package_filter": "security",
                "status": "failed",
                "error_message": "dpkg lock",
            }
        )
        self.assertIn("Welle", hist)
        self.assertIn("dpkg", hist)


if __name__ == "__main__":
    unittest.main()
