"""Tests for standalone / multi-host Proxmox endpoints."""

from __future__ import annotations

import unittest

import httpx

from app.config import Settings
from app.core.discovery import DiscoveryEngine
from app.core.models import EntityKind, EntityStatus, TopologyEntity, TopologySnapshot
from app.core.proxmox import (
    ProxmoxEndpoint,
    ProxmoxNodeUnboundError,
    endpoints_from_settings,
    format_proxmox_api_error,
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
            proxmox_token_secret="a",
            proxmox_2_host="192.168.5.102",
            proxmox_2_token_id="copilot",
            proxmox_2_token_secret="b",
        )
        eps = endpoints_from_settings(s)
        self.assertEqual([e.id for e in eps], ["primary", "extra:2"])
        self.assertEqual(eps[1].host, "192.168.5.102")
        self.assertIn("PVEAPIToken=", eps[1].auth_headers()["Authorization"])

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


if __name__ == "__main__":
    unittest.main()
