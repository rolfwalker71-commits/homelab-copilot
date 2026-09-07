"""Tests for standalone / multi-host Proxmox endpoints."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

import httpx

from app.config import Settings
from app.core.discovery import DiscoveryEngine
from app.core.models import EntityKind, EntityStatus, TopologyEntity, TopologySnapshot
from app.core.tree import build_topology_tree
from app.core.proxmox import (
    ProxmoxEndpoint,
    ProxmoxHostRow,
    ProxmoxNodeUnboundError,
    apply_host_rows_to_settings,
    endpoints_from_settings,
    format_proxmox_api_error,
    format_proxmox_host_error,
    host_rows_from_setup_payload,
    hosts_from_env,
    merge_proxmox_hosts,
    strip_unbound_metrics,
    unbound_message,
)


def _settings(**kwargs: object) -> Settings:
    base = {
        "proxmox_host": "",
        "proxmox_token_secret": "",
        "proxmox_password": "",
        "proxmox_2_host": "",
        "proxmox_2_token_secret": "",
        "proxmox_2_password": "",
    }
    base.update(kwargs)
    return Settings(**base)


class EndpointTests(unittest.TestCase):
    def test_primary_only(self) -> None:
        s = _settings(proxmox_host="192.168.5.101", proxmox_token_secret="sec")
        eps = endpoints_from_settings(s)
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0].id, "primary")
        self.assertEqual(eps[0].base_url, "https://192.168.5.101:8006/api2/json")
        self.assertTrue(s.proxmox_configured)

    def test_primary_and_standalone_second(self) -> None:
        s = _settings(
            proxmox_host="192.168.5.101",
            proxmox_token_id="copilot",
            proxmox_token_secret="sec-a",
            proxmox_2_host="192.168.5.102",
            proxmox_2_token_id="copilot",
            proxmox_2_token_secret="sec-b",
        )
        eps = endpoints_from_settings(s)
        self.assertEqual([e.id for e in eps], ["primary", "extra:2"])
        self.assertEqual(eps[1].host, "192.168.5.102")
        self.assertEqual(
            eps[0].auth_headers()["Authorization"],
            "PVEAPIToken=root@pam!copilot=sec-a",
        )
        self.assertEqual(
            eps[1].auth_headers()["Authorization"],
            "PVEAPIToken=root@pam!copilot=sec-b",
        )

    def test_skip_duplicate_host_port(self) -> None:
        s = _settings(
            proxmox_host="10.0.0.1",
            proxmox_token_secret="a",
            proxmox_2_host="10.0.0.1",
            proxmox_2_token_secret="b",
        )
        self.assertEqual(len(endpoints_from_settings(s)), 1)

    def test_extra_only(self) -> None:
        s = _settings(proxmox_2_host="192.168.5.102", proxmox_2_token_secret="b")
        eps = endpoints_from_settings(s)
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0].id, "extra:2")
        self.assertTrue(s.proxmox_configured)

    def test_not_configured_without_auth(self) -> None:
        s = _settings(proxmox_host="192.168.5.101")
        self.assertFalse(s.proxmox_configured)
        self.assertEqual(endpoints_from_settings(s), [])


class ErrorFormatTests(unittest.TestCase):
    def test_unbound_message(self) -> None:
        self.assertEqual(
            unbound_message("pve01"),
            "Kein API-Zugang — Node ist kein Cluster-Mitglied von pve01",
        )
        err = ProxmoxNodeUnboundError("pve02", "pve01")
        self.assertEqual(
            str(err),
            "Kein API-Zugang — Node ist kein Cluster-Mitglied von pve01",
        )
        self.assertIn("pve01", format_proxmox_api_error(err))

    def test_http_404(self) -> None:
        req = httpx.Request("GET", "https://pve01:8006/api2/json/nodes/pve02/status")
        resp = httpx.Response(404, request=req, json={"message": "No such node 'pve02'"})
        exc = httpx.HTTPStatusError("boom", request=req, response=resp)
        text = format_proxmox_api_error(exc)
        self.assertIn("HTTP 404", text)
        self.assertIn("nicht gefunden", text)
        self.assertIn("pve02", text)

    def test_http_401_setup_hint(self) -> None:
        req = httpx.Request("GET", "https://100.117.60.250:8006/api2/json/nodes")
        resp = httpx.Response(401, request=req)
        exc = httpx.HTTPStatusError("boom", request=req, response=resp)
        text = format_proxmox_api_error(exc)
        self.assertIn("HTTP 401", text)
        self.assertIn("nicht autorisiert", text)
        self.assertIn("Token zurückweisen — in Setup prüfen", text)
        named = format_proxmox_host_error(
            exc, host="100.117.60.250", display_name="pve02"
        )
        self.assertTrue(named.startswith("Proxmox pve02"))
        self.assertIn("100.117.60.250", named)
        self.assertIn("Token zurückweisen — in Setup prüfen", named)

    def test_token_header_strips_whitespace_and_colon(self) -> None:
        ep = ProxmoxEndpoint(
            id="extra:2",
            host="100.117.60.250",
            user="root@pam",
            token_id="root@pam:copilot\n",
            token_secret="  sec-b \n",
        )
        self.assertEqual(
            ep.auth_headers()["Authorization"],
            "PVEAPIToken=root@pam!copilot=sec-b",
        )

    def test_http_403(self) -> None:
        req = httpx.Request("GET", "https://pve01:8006/api2/json/nodes/pve01/status")
        resp = httpx.Response(403, request=req, json={"message": "Permission denied"})
        exc = httpx.HTTPStatusError("boom", request=req, response=resp)
        text = format_proxmox_api_error(exc)
        self.assertIn("403", text)
        self.assertIn("Zugriff verweigert", text)

    def test_connection_refused(self) -> None:
        text = format_proxmox_api_error(httpx.ConnectError("Connection refused"))
        self.assertIn("connection refused", text.lower())

    def test_strip_metrics(self) -> None:
        meta = strip_unbound_metrics({"cpu_pct": 12.0, "mem": 1, "keep": True})
        self.assertNotIn("cpu_pct", meta)
        self.assertNotIn("mem", meta)
        self.assertTrue(meta["keep"])


class RoutingTests(unittest.TestCase):
    def test_unbound_never_uses_primary(self) -> None:
        s = _settings(proxmox_host="192.168.5.101", proxmox_token_secret="a")
        engine = DiscoveryEngine(s)
        engine._unbound_via["pve02"] = "pve01"
        engine._node_endpoints["pve01"] = ProxmoxEndpoint(
            id="primary", host="192.168.5.101", token_secret="a"
        )
        with self.assertRaises(ProxmoxNodeUnboundError) as ctx:
            engine._require_endpoint_for_node("pve02")
        self.assertEqual(
            str(ctx.exception),
            "Kein API-Zugang — Node ist kein Cluster-Mitglied von pve01",
        )

    def test_owned_node_uses_its_endpoint(self) -> None:
        s = _settings(
            proxmox_host="192.168.5.101",
            proxmox_token_secret="a",
            proxmox_2_host="192.168.5.102",
            proxmox_2_token_secret="b",
        )
        engine = DiscoveryEngine(s)
        extra = endpoints_from_settings(s)[1]
        engine._node_endpoints["pve02"] = extra
        self.assertEqual(engine._require_endpoint_for_node("pve02").id, "extra:2")
        self.assertEqual(
            engine._require_endpoint_for_node("pve02").host, "192.168.5.102"
        )

    def test_remember_unbound_from_snapshot(self) -> None:
        s = _settings(proxmox_host="192.168.5.101", proxmox_token_secret="a")
        engine = DiscoveryEngine(s)
        snap = TopologySnapshot(
            refreshed_at="x",
            refreshed_at_iso="x",
            nodes=[
                TopologyEntity(
                    id="node:pve02",
                    kind=EntityKind.NODE,
                    name="pve02",
                    status=EntityStatus.UNKNOWN,
                    meta={"api_unbound": True, "api_via": "pve01"},
                )
            ],
        )
        engine.remember_from_snapshot(snap)
        with self.assertRaises(ProxmoxNodeUnboundError):
            engine._require_endpoint_for_node("pve02")


class MergeTests(unittest.TestCase):
    def test_empty_db_uses_env(self) -> None:
        s = _settings(
            proxmox_host="192.168.5.101",
            proxmox_token_secret="env-a",
            proxmox_2_host="192.168.5.102",
            proxmox_2_token_secret="env-b",
        )
        merged = merge_proxmox_hosts([], s)
        self.assertEqual([r.host for r in merged], ["192.168.5.101", "192.168.5.102"])
        self.assertEqual(merged[1].token_secret, "env-b")

    def test_db_rows_win_over_env(self) -> None:
        s = _settings(
            proxmox_host="10.0.0.1",
            proxmox_token_secret="env-a",
            proxmox_2_host="10.0.0.2",
            proxmox_2_token_secret="env-b",
        )
        db = [
            ProxmoxHostRow(
                slot=1, host="192.168.5.101", token_secret="db-a", label="pve01"
            ),
            ProxmoxHostRow(
                slot=2, host="192.168.5.102", token_secret="db-b", label="pve02"
            ),
        ]
        merged = merge_proxmox_hosts(db, s)
        self.assertEqual([r.host for r in merged], ["192.168.5.101", "192.168.5.102"])
        self.assertEqual(merged[1].token_secret, "db-b")
        apply_host_rows_to_settings(s, merged)
        eps = endpoints_from_settings(s)
        self.assertEqual([e.id for e in eps], ["primary", "extra:2"])
        self.assertEqual(eps[1].host, "192.168.5.102")
        self.assertEqual(s.proxmox_2_host, "192.168.5.102")

    def test_db_primary_only_keeps_env_pve02(self) -> None:
        s = _settings(
            proxmox_host="192.168.5.101",
            proxmox_token_secret="env-a",
            proxmox_2_host="192.168.5.102",
            proxmox_2_token_id="copilot",
            proxmox_2_token_secret="env-b",
        )
        db = [
            ProxmoxHostRow(slot=1, host="192.168.5.101", token_secret="db-a"),
        ]
        merged = merge_proxmox_hosts(db, s)
        apply_host_rows_to_settings(s, merged)
        eps = endpoints_from_settings(s)
        self.assertEqual([e.id for e in eps], ["primary", "extra:2"])
        self.assertEqual(eps[1].host, "192.168.5.102")
        self.assertEqual(eps[1].token_secret, "env-b")
        self.assertEqual(s.proxmox_2_host, "192.168.5.102")
        self.assertEqual(s.proxmox_2_token_secret, "env-b")

    def test_empty_db_token_uses_env_same_host(self) -> None:
        s = _settings(
            proxmox_2_host="100.117.60.250",
            proxmox_2_token_id="copilot",
            proxmox_2_token_secret="env-b",
        )
        db = [
            ProxmoxHostRow(
                slot=2, host="100.117.60.250", token_id="copilot", token_secret=""
            ),
        ]
        merged = merge_proxmox_hosts(db, s)
        self.assertEqual(merged[0].token_secret, "env-b")
        apply_host_rows_to_settings(s, merged)
        ep = endpoints_from_settings(s)[0]
        self.assertEqual(ep.host, "100.117.60.250")
        self.assertEqual(ep.token_secret, "env-b")

    def test_good_db_token_not_overwritten_by_empty_env(self) -> None:
        s = _settings(
            proxmox_2_host="100.117.60.250",
            proxmox_2_token_secret="",
        )
        db = [
            ProxmoxHostRow(
                slot=2,
                host="100.117.60.250",
                token_id="copilot",
                token_secret="db-b",
                label="pve02",
            ),
        ]
        merged = merge_proxmox_hosts(db, s)
        self.assertEqual(merged[0].token_secret, "db-b")
        apply_host_rows_to_settings(s, merged)
        self.assertEqual(endpoints_from_settings(s)[0].token_secret, "db-b")

    def test_setup_save_keeps_blank_secrets(self) -> None:
        previous = hosts_from_env(
            _settings(
                proxmox_host="192.168.5.101",
                proxmox_token_id="copilot",
                proxmox_token_secret="keep-a",
                proxmox_2_host="192.168.5.102",
                proxmox_2_token_secret="keep-b",
            )
        )
        rows = host_rows_from_setup_payload(
            {
                "proxmox_host": "192.168.5.101",
                "proxmox_port": 8006,
                "proxmox_user": "root@pam",
                "proxmox_token_id": "copilot",
                "proxmox_verify_ssl": False,
                "proxmox_2_host": "192.168.5.102",
                "proxmox_2_port": 8006,
                "proxmox_2_user": "root@pam",
                "proxmox_2_token_id": "copilot",
                "proxmox_2_verify_ssl": False,
            },
            previous,
        )
        self.assertEqual(rows[0].token_secret, "keep-a")
        self.assertEqual(rows[1].token_secret, "keep-b")

    def test_setup_clears_slot2_when_empty(self) -> None:
        previous = [
            ProxmoxHostRow(slot=1, host="192.168.5.101", token_secret="a"),
            ProxmoxHostRow(slot=2, host="192.168.5.102", token_secret="b"),
        ]
        rows = host_rows_from_setup_payload(
            {
                "proxmox_host": "192.168.5.101",
                "proxmox_token_secret": "a",
                "proxmox_2_host": "",
            },
            previous,
            include_slot2=True,
        )
        self.assertEqual([r.slot for r in rows], [1])

    def test_setup_keeps_slot3(self) -> None:
        previous = [
            ProxmoxHostRow(slot=1, host="192.168.5.101", token_secret="a"),
            ProxmoxHostRow(slot=3, host="192.168.5.103", token_secret="c", label="pve03"),
        ]
        rows = host_rows_from_setup_payload(
            {"proxmox_host": "192.168.5.101", "proxmox_2_host": "192.168.5.102"},
            previous,
        )
        self.assertEqual(rows[1].token_secret, "")  # new slot2, no previous secret
        self.assertEqual([r.slot for r in rows], [1, 2, 3])
        self.assertEqual(rows[2].host, "192.168.5.103")


class MultiHostDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def _dual_settings(self) -> Settings:
        s = _settings(
            proxmox_host="192.168.5.101",
            proxmox_token_id="copilot",
            proxmox_token_secret="sec-a",
            proxmox_2_host="100.117.60.250",
            proxmox_2_user="root@pam",
            proxmox_2_token_id="copilot",
            proxmox_2_token_secret="sec-b",
        )
        apply_host_rows_to_settings(s, hosts_from_env(s))
        return s

    async def test_host2_queries_use_host2_token_only(self) -> None:
        s = self._dual_settings()
        engine = DiscoveryEngine(s)
        seen: list[tuple[str, str, str]] = []

        async def _get(client, path, headers):
            ep = getattr(client, "_pve_endpoint", None)
            auth = str((headers or {}).get("Authorization") or "")
            seen.append((ep.host if ep else "", str(path), auth))
            if str(path) == "/nodes":
                node = "pve01" if ep and ep.id == "primary" else "pve02"
                return [{"node": node, "status": "online"}]
            if "cluster/resources" in str(path):
                return []
            if str(path).endswith("/lxc") or str(path).endswith("/qemu"):
                return []
            if str(path).endswith("/status"):
                return {"status": "online"}
            return {}

        engine._proxmox_get = AsyncMock(side_effect=_get)
        engine._probe_token_acl = AsyncMock(return_value=[])
        engine._enrich_node_ips = AsyncMock()
        nodes, _guests, errors = await engine._discover_proxmox()
        self.assertFalse(any("401" in e for e in errors))
        self.assertEqual(sorted(n.name for n in nodes), ["pve01", "pve02"])
        host2 = [c for c in seen if c[0] == "100.117.60.250"]
        host1 = [c for c in seen if c[0] == "192.168.5.101"]
        self.assertTrue(host2)
        self.assertTrue(all("sec-b" in auth and "sec-a" not in auth for _h, _p, auth in host2))
        self.assertTrue(all("sec-a" in auth and "sec-b" not in auth for _h, _p, auth in host1))
        self.assertEqual(engine._node_endpoints["pve02"].host, "100.117.60.250")
        self.assertEqual(engine._node_endpoints["pve02"].token_secret, "sec-b")

    async def test_401_keeps_host2_in_inventory_list(self) -> None:
        s = self._dual_settings()
        engine = DiscoveryEngine(s)
        extra = endpoints_from_settings(s)[1]
        engine._node_endpoints["pve02"] = extra

        async def _get(client, path, headers):
            ep = getattr(client, "_pve_endpoint", None)
            if ep and ep.id != "primary":
                req = httpx.Request("GET", f"{ep.base_url}{path}")
                raise httpx.HTTPStatusError(
                    "no",
                    request=req,
                    response=httpx.Response(401, request=req),
                )
            if str(path) == "/nodes":
                return [{"node": "pve01", "status": "online"}]
            if "cluster/resources" in str(path):
                return []
            if str(path).endswith("/lxc") or str(path).endswith("/qemu"):
                return []
            return {}

        engine._proxmox_get = AsyncMock(side_effect=_get)
        engine._probe_token_acl = AsyncMock(return_value=[])
        engine._enrich_node_ips = AsyncMock()
        nodes, _guests, errors = await engine._discover_proxmox()
        names = [n.name for n in nodes]
        self.assertIn("pve01", names)
        self.assertIn("pve02", names)
        stub = next(n for n in nodes if n.name == "pve02")
        self.assertEqual(stub.status, EntityStatus.ERROR)
        self.assertTrue((stub.meta or {}).get("api_auth_error"))
        self.assertTrue(any("pve02" in e for e in errors))
        self.assertTrue(any("Token zurückweisen — in Setup prüfen" in e for e in errors))
        self.assertTrue(any("100.117.60.250" in e for e in errors))
        tree = build_topology_tree(
            TopologySnapshot(
                refreshed_at="x",
                refreshed_at_iso="x",
                nodes=nodes,
                errors=errors,
                proxmox_configured=True,
            )
        )
        self.assertEqual([n["name"] for n in tree["nodes"]], ["pve01", "pve02"])


if __name__ == "__main__":
    unittest.main()
