"""Live Hosts-rail filter: do not invent ``lxc-114`` from incomplete PVE rows."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

import httpx

from app.config import Settings
from app.core.discovery import (
    DiscoveryEngine,
    resource_guest_name,
    should_emit_rail_guest,
)
from app.core.models import EntityKind, EntityStatus


def _engine() -> DiscoveryEngine:
    return DiscoveryEngine(
        Settings(
            proxmox_host="",
            proxmox_token_secret="",
            proxmox_password="",
        )
    )


class ResourceNameTests(unittest.TestCase):
    def test_nameless_resource_does_not_invent_lxc_114(self) -> None:
        raw = {"vmid": 114, "type": "lxc", "status": "", "tags": ""}
        self.assertEqual(resource_guest_name(raw), "")
        self.assertFalse(
            should_emit_rail_guest(
                kind=EntityKind.LXC,
                status=EntityStatus.UNKNOWN,
                name="",
                config_ok=False,
            )
        )

    def test_hostname_from_config_when_resource_name_empty(self) -> None:
        raw = {"vmid": 113, "type": "lxc", "status": "running"}
        cfg = {"hostname": "vaultwarden"}
        self.assertEqual(resource_guest_name(raw, cfg=cfg), "vaultwarden")

    def test_unknown_without_config_rejected(self) -> None:
        self.assertFalse(
            should_emit_rail_guest(
                kind="lxc",
                status=EntityStatus.UNKNOWN,
                name="lxc-114",
                config_ok=False,
            )
        )

    def test_config_595_dropped_even_if_stopped(self) -> None:
        self.assertFalse(
            should_emit_rail_guest(
                kind=EntityKind.LXC,
                status=EntityStatus.STOPPED,
                name="leftover",
                config_ok=False,
                config_http=595,
            )
        )

    def test_config_500_dropped(self) -> None:
        self.assertFalse(
            should_emit_rail_guest(
                kind=EntityKind.QEMU,
                status=EntityStatus.RUNNING,
                name="ghost",
                config_ok=False,
                config_http=500,
            )
        )

    def test_template_dropped(self) -> None:
        self.assertFalse(
            should_emit_rail_guest(
                kind=EntityKind.LXC,
                status=EntityStatus.STOPPED,
                name="ubuntu-tmpl",
                config_ok=True,
                template=True,
            )
        )

    def test_live_with_config_kept(self) -> None:
        self.assertTrue(
            should_emit_rail_guest(
                kind=EntityKind.LXC,
                status=EntityStatus.STOPPED,
                name="adguard",
                config_ok=True,
                config_http=200,
            )
        )

    def test_acl_403_keeps_named_running_guest(self) -> None:
        self.assertTrue(
            should_emit_rail_guest(
                kind=EntityKind.LXC,
                status=EntityStatus.RUNNING,
                name="immich",
                config_ok=False,
                config_http=403,
            )
        )


class EnrichGuestTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_595_returns_none(self) -> None:
        engine = _engine()
        req = httpx.Request("GET", "https://pve02:8006/api2/json/nodes/pve02/lxc/118/config")
        resp = httpx.Response(595, request=req, json={"message": "got timeout"})
        engine._proxmox_get = AsyncMock(
            side_effect=httpx.HTTPStatusError("timeout", request=req, response=resp)
        )
        guest = await engine._enrich_guest(
            MagicMock(),
            {},
            "pve02",
            {"vmid": 118, "type": "lxc", "status": "stopped", "name": "lxc-118"},
            EntityKind.LXC,
            "jetzt",
            "2026-09-05T19:00:00Z",
        )
        self.assertIsNone(guest)

    async def test_unknown_nameless_not_invented_even_without_http(self) -> None:
        engine = _engine()
        engine._proxmox_get = AsyncMock(side_effect=RuntimeError("no config"))
        guest = await engine._enrich_guest(
            MagicMock(),
            {},
            "pve01",
            {"vmid": 114, "type": "lxc", "status": "", "tags": ""},
            EntityKind.LXC,
            "jetzt",
            "2026-09-05T19:00:00Z",
        )
        self.assertIsNone(guest)


class PlainGuestTests(unittest.TestCase):
    def test_plain_unknown_nameless_is_none(self) -> None:
        guest = _engine()._guest_entity_plain(
            "pve01",
            {"vmid": 114, "type": "lxc", "status": "", "tags": "docker"},
            EntityKind.LXC,
            "jetzt",
            "2026-09-05T19:00:00Z",
        )
        self.assertIsNone(guest)

    def test_plain_running_named_kept(self) -> None:
        guest = _engine()._guest_entity_plain(
            "pve02",
            {"vmid": 105, "type": "lxc", "status": "running", "name": "paperless"},
            EntityKind.LXC,
            "jetzt",
            "2026-09-05T19:00:00Z",
        )
        assert guest is not None
        self.assertEqual(guest.name, "paperless")
        self.assertEqual(guest.meta.get("pve_source"), "pve02")


if __name__ == "__main__":
    unittest.main()
