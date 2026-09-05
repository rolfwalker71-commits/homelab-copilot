"""Live Hosts-rail filter: do not invent ``lxc-114`` from incomplete PVE rows."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

import httpx

from app.config import Settings
from app.core.discovery import (
    DiscoveryEngine,
    ips_from_iface_payload,
    ips_from_lxc_config,
    ips_from_qemu_config,
    prefer_guest_ipv4s,
    resource_guest_name,
    should_emit_rail_guest,
)
from app.core.models import EntityKind, EntityStatus, TopologyEntity, TopologySnapshot


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


class GuestIpParseTests(unittest.TestCase):
    def test_static_net0_ip(self) -> None:
        cfg = {
            "net0": "name=eth0,bridge=vmbr0,ip=192.168.5.100/24,gw=192.168.5.1",
        }
        self.assertEqual(ips_from_lxc_config(cfg), ["192.168.5.100"])

    def test_dhcp_and_manual_net0_empty(self) -> None:
        self.assertEqual(
            ips_from_lxc_config(
                {"net0": "name=eth0,bridge=vmbr0,ip=dhcp,ip6=dhcp"}
            ),
            [],
        )
        self.assertEqual(
            ips_from_lxc_config(
                {"net0": "name=eth0,bridge=vmbr0,ip=manual,hwaddr=BC:24:11:00:00:01"}
            ),
            [],
        )
        self.assertEqual(
            ips_from_lxc_config({"net0": "name=eth0,bridge=vmbr0,ip=DHCP"}),
            [],
        )

    def test_dhcp_net0_static_net1(self) -> None:
        cfg = {
            "net0": "name=eth0,bridge=vmbr0,ip=dhcp",
            "net1": "name=eth1,bridge=vmbr1,ip=10.8.0.2/24",
        }
        self.assertEqual(ips_from_lxc_config(cfg), ["10.8.0.2"])

    def test_qemu_ipconfig_static_skips_dhcp(self) -> None:
        self.assertEqual(
            ips_from_qemu_config({"ipconfig0": "ip=192.168.5.80/24,gw=192.168.5.1"}),
            ["192.168.5.80"],
        )
        self.assertEqual(ips_from_qemu_config({"ipconfig0": "ip=dhcp"}), [])

    def test_interfaces_inet_prefers_lan(self) -> None:
        payload = [
            {"name": "lo", "inet": "127.0.0.1/8"},
            {
                "name": "eth0",
                "inet": "169.254.12.4/16",
                "inet6": "fe80::1/64",
            },
            {"name": "eth0", "inet": "192.168.5.50/24"},
            {"name": "eth1", "inet": "10.0.0.9/24"},
        ]
        self.assertEqual(ips_from_iface_payload(payload), ["192.168.5.50", "10.0.0.9"])

    def test_interfaces_ip_addresses_agent_shape(self) -> None:
        payload = {
            "result": [
                {
                    "name": "lo",
                    "ip-addresses": [
                        {"ip-address": "127.0.0.1", "ip-address-type": "ipv4"},
                    ],
                },
                {
                    "name": "eth0",
                    "ip-addresses": [
                        {"ip-address": "169.254.1.1", "ip-address-type": "ipv4"},
                        {"ip-address": "192.168.5.50", "ip-address-type": "ipv4"},
                        {"ip-address": "fe80::1", "ip-address-type": "ipv6"},
                    ],
                },
            ]
        }
        self.assertEqual(ips_from_iface_payload(payload), ["192.168.5.50"])

    def test_prefer_lan_over_other_global(self) -> None:
        self.assertEqual(
            prefer_guest_ipv4s(["10.0.0.2", "192.168.1.10", "169.254.1.1", "127.0.0.1"]),
            ["192.168.1.10", "10.0.0.2"],
        )


class GuestLiveStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_current_running_keeps_uptime(self) -> None:
        engine = _engine()
        client = MagicMock()
        client.aclose = AsyncMock()
        engine._proxmox_authed_client = AsyncMock(return_value=(client, {}))

        async def _get(_c, path, _h):
            if str(path).endswith("/status/current"):
                return {
                    "status": "running",
                    "uptime": 84,
                    "cpu": 0.012,
                    "cpus": 2,
                    "mem": 256 * 1024 * 1024,
                    "maxmem": 2 * 1024 * 1024 * 1024,
                    "name": "n8n",
                }
            if str(path).endswith("/config"):
                return {"hostname": "n8n", "unprivileged": 1}
            return {}

        engine._proxmox_get = AsyncMock(side_effect=_get)
        live = await engine.fetch_guest_status("lxc:pve01:122")
        self.assertEqual(live["status"], "running")
        self.assertEqual(live["uptime"], 84)
        self.assertEqual(live["cpu_pct"], 1.2)
        self.assertTrue(live["unprivileged"])
        self.assertEqual(live["ip_addresses"], [])
        client.aclose.assert_awaited()

    async def test_dhcp_lxc_uses_pve02_interfaces(self) -> None:
        engine = _engine()
        client = MagicMock()
        client.aclose = AsyncMock()
        engine._proxmox_authed_client = AsyncMock(return_value=(client, {}))
        paths: list[str] = []

        async def _get(_c, path, _h):
            paths.append(str(path))
            if str(path).endswith("/status/current"):
                return {"status": "running", "uptime": 360, "name": "elementsynapse"}
            if str(path).endswith("/config"):
                return {
                    "hostname": "elementsynapse",
                    "unprivileged": 1,
                    "net0": "name=eth0,bridge=vmbr0,ip=dhcp",
                }
            if str(path).endswith("/interfaces"):
                return [
                    {"name": "lo", "inet": "127.0.0.1/8"},
                    {"name": "eth0", "inet": "192.168.5.200/24"},
                ]
            return {}

        engine._proxmox_get = AsyncMock(side_effect=_get)
        live = await engine.fetch_guest_status("lxc:pve02:100")
        engine._proxmox_authed_client.assert_awaited_with(node="pve02")
        self.assertEqual(live["ip_addresses"], ["192.168.5.200"])
        self.assertTrue(any(p.endswith("/nodes/pve02/lxc/100/interfaces") for p in paths))
        self.assertFalse(any("pve01" in p for p in paths))

    async def test_static_config_skips_interfaces(self) -> None:
        engine = _engine()
        client = MagicMock()
        client.aclose = AsyncMock()
        engine._proxmox_authed_client = AsyncMock(return_value=(client, {}))
        paths: list[str] = []

        async def _get(_c, path, _h):
            paths.append(str(path))
            if str(path).endswith("/status/current"):
                return {"status": "running", "uptime": 10, "name": "adguard"}
            if str(path).endswith("/config"):
                return {
                    "hostname": "adguard",
                    "net0": "name=eth0,bridge=vmbr0,ip=192.168.5.53/24,gw=192.168.5.1",
                }
            raise AssertionError(f"unexpected GET {path}")

        engine._proxmox_get = AsyncMock(side_effect=_get)
        live = await engine.fetch_guest_status("lxc:pve01:105")
        self.assertEqual(live["ip_addresses"], ["192.168.5.53"])
        self.assertFalse(any(p.endswith("/interfaces") for p in paths))

    async def test_missing_cpu_does_not_invent_pct(self) -> None:
        live = _engine()._guest_live_from_pve(
            "lxc:pve01:122",
            "lxc",
            "pve01",
            122,
            {"status": "running", "uptime": 12, "name": "n8n"},
            {"hostname": "n8n"},
        )
        self.assertEqual(live["status"], "running")
        self.assertEqual(live["uptime"], 12)
        self.assertIsNone(live.get("cpu_pct"))

    async def test_power_already_running_returns_live(self) -> None:
        engine = _engine()
        client = MagicMock()
        client.aclose = AsyncMock()
        engine._proxmox_authed_client = AsyncMock(return_value=(client, {}))
        req = httpx.Request("POST", "https://pve/api2/json/nodes/pve01/lxc/122/status/start")
        resp = httpx.Response(
            500, request=req, json={"message": "CT 122 is already running"}
        )
        engine._proxmox_post = AsyncMock(
            side_effect=httpx.HTTPStatusError("err", request=req, response=resp)
        )
        engine._read_guest_live = AsyncMock(
            return_value={
                "guest_id": "lxc:pve01:122",
                "status": "running",
                "uptime": 40,
            }
        )
        out = await engine.guest_power("lxc:pve01:122", "start")
        self.assertTrue(out["ok"])
        self.assertTrue(out["already"])
        self.assertIn("läuft bereits", out["message"])
        self.assertEqual(out["live"]["status"], "running")
        self.assertEqual(out["live"]["uptime"], 40)


class PatchGuestLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_patches_status_and_uptime(self) -> None:
        from app.core.topology import TopologyStore

        store = TopologyStore.__new__(TopologyStore)
        store._snapshot = TopologySnapshot(
            refreshed_at="x",
            refreshed_at_iso="x",
            guests=[
                TopologyEntity(
                    id="lxc:pve01:122",
                    kind=EntityKind.LXC,
                    name="n8n",
                    status=EntityStatus.STOPPED,
                    meta={"uptime": 0, "cpu_pct": 0, "mem_pct": 0},
                )
            ],
        )
        async def _save(snapshot):
            store._snapshot = snapshot

        store.save = AsyncMock(side_effect=_save)
        await store.patch_guest_live(
            {
                "guest_id": "lxc:pve01:122",
                "status": "running",
                "uptime": 40,
                "cpu_pct": 1.2,
                "mem": 100,
                "maxmem": 2000,
                "mem_pct": 5.0,
            }
        )
        guest = store.snapshot.guests[0]
        self.assertEqual(guest.status, EntityStatus.RUNNING)
        self.assertEqual(guest.meta["uptime"], 40)
        self.assertEqual(guest.meta["cpu_pct"], 1.2)
        store.save.assert_awaited()

    async def test_patches_ip_without_clearing_when_empty(self) -> None:
        from app.core.topology import TopologyStore

        store = TopologyStore.__new__(TopologyStore)
        store._snapshot = TopologySnapshot(
            refreshed_at="x",
            refreshed_at_iso="x",
            guests=[
                TopologyEntity(
                    id="lxc:pve02:100",
                    kind=EntityKind.LXC,
                    name="elementsynapse",
                    status=EntityStatus.RUNNING,
                    ip_addresses=["192.168.5.99"],
                )
            ],
        )

        async def _save(snapshot):
            store._snapshot = snapshot

        store.save = AsyncMock(side_effect=_save)
        await store.patch_guest_live(
            {
                "guest_id": "lxc:pve02:100",
                "status": "running",
                "ip_addresses": ["192.168.5.200"],
            }
        )
        self.assertEqual(store.snapshot.guests[0].ip_addresses, ["192.168.5.200"])
        await store.patch_guest_live(
            {"guest_id": "lxc:pve02:100", "status": "running", "ip_addresses": []}
        )
        self.assertEqual(store.snapshot.guests[0].ip_addresses, ["192.168.5.200"])


if __name__ == "__main__":
    unittest.main()
