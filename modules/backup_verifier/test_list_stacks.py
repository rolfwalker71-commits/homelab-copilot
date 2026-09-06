"""Backup dropdown discovery matches topology compose stacks."""

from __future__ import annotations

import asyncio
import unittest

from app.core.models import EntityKind, EntityStatus, TopologyEntity, TopologySnapshot
from backup_verifier.backup import describe_backup_stacks, list_backup_stacks


def _snap(*, guests=None, containers=None, hosts=None) -> TopologySnapshot:
    return TopologySnapshot(
        refreshed_at="jetzt",
        refreshed_at_iso="2026-09-06T18:00:00Z",
        nodes=[
            TopologyEntity(
                id="node:pve01",
                kind=EntityKind.NODE,
                name="pve01",
                status=EntityStatus.RUNNING,
            )
        ],
        guests=guests or [],
        hosts=hosts or [],
        containers=containers or [],
    )


def _guest(name: str, vmid: int, *, status: EntityStatus = EntityStatus.RUNNING) -> TopologyEntity:
    return TopologyEntity(
        id=f"lxc:pve01:{vmid}",
        kind=EntityKind.LXC,
        name=name,
        hostname=name,
        status=status,
        node="pve01",
        vmid=vmid,
        ip_addresses=["10.0.0.1"] if status == EntityStatus.RUNNING else [],
    )


def _ctr(parent: str, name: str, project: str | None) -> TopologyEntity:
    labels = {}
    meta = {}
    if project:
        labels["com.docker.compose.project"] = project
        meta["compose_project"] = project
    return TopologyEntity(
        id=f"docker:{parent}:{name}",
        kind=EntityKind.DOCKER,
        name=name,
        status=EntityStatus.RUNNING,
        parent_id=parent,
        labels=labels,
        meta=meta,
    )


class DescribeBackupStacksTests(unittest.TestCase):
    def test_compose_stacks_are_eligible(self) -> None:
        guest = _guest("paperlessngx", 105)
        snap = _snap(
            guests=[guest],
            containers=[
                _ctr(guest.id, "paperless-web-1", "paperless"),
                _ctr(guest.id, "paperless-db-1", "paperless"),
            ],
        )
        out = describe_backup_stacks(snap)
        self.assertEqual(len(out["stacks"]), 1)
        self.assertEqual(out["stacks"][0]["stack"], "paperless")
        self.assertEqual(out["excluded"], [])

    def test_prefix_stack_is_excluded_not_invented(self) -> None:
        guest = _guest("box", 10)
        snap = _snap(
            guests=[guest],
            containers=[
                _ctr(guest.id, "wallstreet-frontend-1", None),
                _ctr(guest.id, "wallstreet-backend-1", None),
            ],
        )
        out = describe_backup_stacks(snap)
        self.assertEqual(out["stacks"], [])
        self.assertEqual(len(out["excluded"]), 1)
        self.assertEqual(out["excluded"][0]["reason_code"], "prefix")
        self.assertEqual(asyncio.run(list_backup_stacks(snap)), [])

    def test_stopped_guest_note(self) -> None:
        snap = _snap(guests=[_guest("adguard", 130, status=EntityStatus.STOPPED)])
        out = describe_backup_stacks(snap)
        self.assertTrue(out["notes"])
        self.assertIn("adguard", out["notes"][0])


if __name__ == "__main__":
    unittest.main()
