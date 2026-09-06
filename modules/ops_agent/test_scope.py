"""Host-matrix add/remove and pre-image snap cleanup — ohne LLM, ohne Proxmox."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ops_agent.engine import OpsEngine
from ops_agent.hosts import collect_live_hosts, overlay_local_scope, split_inventory_changes
from ops_agent.image_snaps import (
    ImageSnap,
    remember_after_image,
    snap_from_job_result,
    snap_to_delete_before_next,
)
from ops_agent.policy import ConfirmPolicy, apply_interview, in_job_scope
from ops_agent.store import OpsStore


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


class InterviewKeepsScopeTests(unittest.TestCase):
    def test_policy_save_without_scope_keeps_lists(self) -> None:
        current = ConfirmPolicy(
            patch_scope_ids=["lxc:pve:1", "qemu:pve:2"],
            image_scope_ids=["lxc:pve:1"],
        )
        merged = apply_interview(
            current,
            confirm_kernel_docker=True,
            confirm_new_guest_backup=True,
            confirm_production=False,
            confirm_nothing=False,
        )
        self.assertEqual(merged.patch_scope_ids, ["lxc:pve:1", "qemu:pve:2"])
        self.assertEqual(merged.image_scope_ids, ["lxc:pve:1"])
        self.assertTrue(merged.answered)

    def test_explicit_empty_scope_is_still_allowed(self) -> None:
        current = ConfirmPolicy(patch_scope_ids=["lxc:1"], image_scope_ids=["lxc:2"])
        merged = apply_interview(
            current,
            confirm_kernel_docker=True,
            confirm_new_guest_backup=False,
            confirm_production=False,
            confirm_nothing=False,
            patch_scope_ids=[],
            image_scope_ids=[],
        )
        self.assertEqual(merged.patch_scope_ids, [])
        self.assertEqual(merged.image_scope_ids, [])


class OverlayTicksTests(unittest.TestCase):
    def test_dirty_rebuild_keeps_unsaved_ticks(self) -> None:
        server = [
            {"id": "lxc:pve:1", "name": "adguard", "patch": False, "image": False},
            {"id": "qemu:pve:2", "name": "mail", "patch": False, "image": False},
        ]
        out = overlay_local_scope(
            server,
            local_patch=["lxc:pve:1"],
            local_image=["qemu:pve:2"],
            dirty=True,
        )
        adg = next(h for h in out if h["id"] == "lxc:pve:1")
        mail = next(h for h in out if h["id"] == "qemu:pve:2")
        self.assertTrue(adg["patch"])
        self.assertFalse(adg["image"])
        self.assertFalse(mail["patch"])
        self.assertTrue(mail["image"])

    def test_clean_rebuild_uses_server_ticks(self) -> None:
        server = [{"id": "lxc:pve:1", "patch": False, "image": True}]
        out = overlay_local_scope(
            server,
            local_patch=["lxc:pve:1"],
            local_image=[],
            dirty=False,
        )
        self.assertFalse(out[0]["patch"])
        self.assertTrue(out[0]["image"])

    def test_overlay_matches_id_case_insensitively(self) -> None:
        server = [{"id": "LXC:PVE:1", "patch": False, "image": False}]
        out = overlay_local_scope(
            server, local_patch=["lxc:pve:1"], local_image=[], dirty=True
        )
        self.assertTrue(out[0]["patch"])


class BoardUiContractTests(unittest.TestCase):
    def test_board_auto_saves_and_protects_dirty_ticks(self) -> None:
        html = Path(__file__).resolve().parent.joinpath("templates/board.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("scopeDirty", html)
        self.assertIn("overlayDirtyTicks", html)
        self.assertIn("flushScopeSave", html)
        self.assertIn("scheduleScopeSave", html)
        self.assertIn("input[data-scope]", html)
        self.assertIn("await flushScopeSave()", html)
        self.assertIn("Haken speichern sich sofort", html)
        self.assertIn("nextSlotOpen", html)
        self.assertIn("renderNextList", html)
        self.assertIn("data-slot", html)
        self.assertIn("Jetzt starten", html)
        self.assertNotIn("Übernommen aus dem bestehenden Backup-Zeitplan", html)
        policy_block = html.split("ops-policy-form")[-1]
        self.assertNotIn("patch_scope_ids", policy_block.split("ops-enabled")[0])


class _Ent:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


class _Snap:
    def __init__(self) -> None:
        self.guests = [
            _Ent(id="lxc:pve:1", kind="lxc", name="adguard", status="stopped", node="pve"),
            _Ent(id="qemu:pve:2", kind="qemu", name="mail", status="running", node="pve"),
        ]
        self.hosts = [
            _Ent(id="host:pve", kind="host", name="pve", status="running", node="pve"),
        ]


def _engine(store: OpsStore, snap: _Snap | None = None) -> OpsEngine:
    async def _empty(*_a: object, **_k: object) -> list:
        return []

    async def _start_patch(_row: dict) -> tuple[bool, str, str | None]:
        return False, "unused", None

    async def _start_backup(_row: dict) -> str | None:
        return None

    return OpsEngine(
        store,
        get_snapshot=lambda: snap if snap is not None else _Snap(),
        get_backup_store=lambda: None,
        list_backup_stacks=_empty,
        hosts_from_store=_empty,
        start_backup=_start_backup,
        start_patch=_start_patch,
        list_backup_jobs=lambda: [],
        list_patch_jobs=lambda: [],
    )


class ScopePersistEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = OpsStore(Path(self.tmp.name) / "ops.db")
        await self.store.connect()
        self.engine = _engine(self.store)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.tmp.cleanup()

    def _host(self, board: dict, tid: str) -> dict:
        return next(h for h in board["hosts"] if h["id"] == tid)

    async def test_save_scope_reload_still_checked(self) -> None:
        first = await self.engine.board()
        self.assertFalse(self._host(first, "lxc:pve:1")["patch"])
        await self.engine.save_scope(
            patch_scope_ids=["lxc:pve:1"],
            image_scope_ids=["qemu:pve:2"],
        )
        again = await self.engine.board()
        adg = self._host(again, "lxc:pve:1")
        mail = self._host(again, "qemu:pve:2")
        self.assertTrue(adg["patch"])
        self.assertFalse(adg["image"])
        self.assertFalse(mail["patch"])
        self.assertTrue(mail["image"])
        self.assertEqual(again["scope"]["patch_ids"], ["lxc:pve:1"])
        self.assertEqual(again["scope"]["image_ids"], ["qemu:pve:2"])

    async def test_policy_save_does_not_clear_scope(self) -> None:
        await self.engine.save_scope(
            patch_scope_ids=["lxc:pve:1"],
            image_scope_ids=["lxc:pve:1"],
        )
        current = await self.store.get_policy()
        merged = apply_interview(
            current,
            confirm_kernel_docker=True,
            confirm_new_guest_backup=True,
            confirm_production=False,
            confirm_nothing=False,
        )
        await self.store.save_policy(merged)
        board = await self.engine.board()
        adg = self._host(board, "lxc:pve:1")
        self.assertTrue(adg["patch"])
        self.assertTrue(adg["image"])
        pol = await self.store.get_policy()
        self.assertEqual(pol.patch_scope_ids, ["lxc:pve:1"])
        self.assertEqual(pol.image_scope_ids, ["lxc:pve:1"])

    async def test_seed_known_does_not_check_hosts(self) -> None:
        created = await self.engine.reconcile_hosts()
        self.assertEqual(created, [])
        board = await self.engine.board()
        self.assertGreaterEqual(len(board["hosts"]), 2)
        self.assertFalse(any(h.get("patch") or h.get("image") for h in board["hosts"]))
        pol = await self.store.get_policy()
        self.assertEqual(pol.patch_scope_ids, [])
        self.assertEqual(pol.image_scope_ids, [])

    async def test_new_host_not_silently_added_to_scope(self) -> None:
        await self.engine.reconcile_hosts()
        await self.engine.save_scope(patch_scope_ids=["lxc:pve:1"], image_scope_ids=[])
        extra = _Snap()
        extra.guests.append(
            _Ent(id="lxc:pve:9", kind="lxc", name="neu", status="running", node="pve")
        )
        engine = _engine(self.store, snap=extra)
        prompts = await engine.reconcile_hosts()
        self.assertTrue(any(p.get("target_id") == "lxc:pve:9" for p in prompts))
        pol = await self.store.get_policy()
        self.assertEqual(pol.patch_scope_ids, ["lxc:pve:1"])
        self.assertNotIn("lxc:pve:9", pol.patch_scope_ids)
        matrix = await engine.scope_matrix()
        neu = next(h for h in matrix["hosts"] if h["id"] == "lxc:pve:9")
        self.assertFalse(neu["patch"])
        self.assertFalse(neu["image"])


if __name__ == "__main__":
    unittest.main()
