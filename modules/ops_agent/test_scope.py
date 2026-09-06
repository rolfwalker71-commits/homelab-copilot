"""Host-matrix add/remove and pre-image snap cleanup — ohne LLM, ohne Proxmox."""

from __future__ import annotations

import unittest

from ops_agent.hosts import collect_live_hosts, split_inventory_changes
from ops_agent.image_snaps import (
    ImageSnap,
    remember_after_image,
    snap_from_job_result,
    snap_to_delete_before_next,
)
from ops_agent.policy import ConfirmPolicy, in_job_scope


class ImageSnapQueue:
    def __init__(self) -> None:
        self.last_ok: ImageSnap | None = None
        self.deleted: list[ImageSnap] = []
        self.kept: list[ImageSnap] = []

    def start_next(self) -> ImageSnap | None:
        doomed = snap_to_delete_before_next(self.last_ok)
        if doomed:
            self.deleted.append(doomed)
            self.last_ok = None
        return doomed

    def finish(self, *, ok: bool, created: ImageSnap | None) -> None:
        if not ok and created:
            self.kept.append(created)
        self.last_ok = remember_after_image(self.last_ok, ok=ok, created=created)


class ImageSnapTests(unittest.TestCase):
    def test_success_deletes_snap_before_next(self) -> None:
        q = ImageSnapQueue()
        self.assertIsNone(q.start_next())
        q.finish(ok=True, created=ImageSnap("lxc:1", "hlops-a"))
        doomed = q.start_next()
        self.assertIsNotNone(doomed)
        assert doomed is not None
        self.assertEqual(doomed.name, "hlops-a")
        self.assertEqual(q.deleted[0].target_id, "lxc:1")
        q.finish(ok=True, created=ImageSnap("lxc:2", "hlops-b"))
        self.assertEqual(q.last_ok.name if q.last_ok else None, "hlops-b")

    def test_failed_image_keeps_snap(self) -> None:
        q = ImageSnapQueue()
        q.finish(ok=True, created=ImageSnap("lxc:1", "hlops-a"))
        q.start_next()
        q.finish(ok=False, created=ImageSnap("lxc:2", "hlops-fail"))
        self.assertEqual([s.name for s in q.kept], ["hlops-fail"])
        self.assertNotIn("hlops-fail", [s.name for s in q.deleted])
        self.assertIsNone(q.start_next())

    def test_job_result_skips_manual(self) -> None:
        snap = snap_from_job_result(
            "manual:1", {"snapshot": {"skipped": True, "reason": "manual"}}
        )
        self.assertIsNone(snap)
        snap = snap_from_job_result(
            "lxc:pve:1", {"snapshot": {"skipped": False, "name": "hlops-x"}}
        )
        self.assertEqual(snap, ImageSnap("lxc:pve:1", "hlops-x"))


class InventoryChangeTests(unittest.TestCase):
    def test_new_and_removed_and_returned(self) -> None:
        appeared, disappeared, returned = split_inventory_changes(
            live_ids={"lxc:new", "lxc:stay", "lxc:back"},
            known_present_ids={"lxc:stay", "lxc:old"},
            known_gone_ids={"lxc:back"},
            pending_ids=set(),
        )
        self.assertEqual(appeared, {"lxc:new"})
        self.assertEqual(disappeared, {"lxc:old"})
        self.assertEqual(returned, {"lxc:back"})

    def test_pending_not_reprompted(self) -> None:
        appeared, disappeared, _returned = split_inventory_changes(
            live_ids={"lxc:new"},
            known_present_ids=set(),
            known_gone_ids=set(),
            pending_ids={"lxc:new"},
        )
        self.assertEqual(appeared, set())
        self.assertEqual(disappeared, set())

    def test_collect_includes_stopped_guests(self) -> None:
        class _Ent:
            def __init__(self, **kw: object) -> None:
                self.__dict__.update(kw)

        class _Snap:
            guests = [
                _Ent(
                    id="lxc:pve:1",
                    kind="lxc",
                    name="mail",
                    status="stopped",
                    node="pve",
                ),
                _Ent(
                    id="qemu:pve:2",
                    kind="qemu",
                    name="win",
                    status="running",
                    node="pve",
                ),
            ]
            hosts = []

        rows = collect_live_hosts(_Snap(), [{"id": "manual:9", "name": "nas", "kind": "manual"}])
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, ["lxc:pve:1", "manual:9", "qemu:pve:2"])
        mail = next(r for r in rows if r["id"] == "lxc:pve:1")
        self.assertFalse(mail["online"])
        self.assertTrue(mail["present"])


class JobScopeTests(unittest.TestCase):
    def test_separate_lists_and_empty_means_none(self) -> None:
        p = ConfirmPolicy(patch_scope_ids=["lxc:1"], image_scope_ids=["lxc:2"])
        self.assertTrue(in_job_scope(p, kind="patch", bucket="security", target_id="lxc:1"))
        self.assertFalse(in_job_scope(p, kind="patch", bucket="images", target_id="lxc:1"))
        self.assertTrue(in_job_scope(p, kind="patch", bucket="images", target_id="lxc:2"))
        self.assertFalse(in_job_scope(p, kind="patch", bucket="security", target_id="lxc:2"))
        empty = ConfirmPolicy()
        self.assertFalse(in_job_scope(empty, kind="patch", bucket="security", target_id="lxc:1"))
        self.assertTrue(in_job_scope(empty, kind="backup", target_id="lxc:1"))


if __name__ == "__main__":
    unittest.main()
