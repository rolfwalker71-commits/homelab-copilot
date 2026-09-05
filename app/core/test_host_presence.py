"""Unit tests for Hosts Online / Offline split."""

from __future__ import annotations

import unittest

from app.core.host_presence import (
    check_url_host,
    health_down_entity_ids,
    is_power_online,
    split_bagel_arcs,
    summarize_host_presence,
)
from app.core.models import EntityKind, EntityStatus, TopologyEntity


def _ent(
    eid: str,
    *,
    status: EntityStatus = EntityStatus.RUNNING,
    ips: list[str] | None = None,
    hostname: str = "",
    kind: EntityKind = EntityKind.LXC,
) -> TopologyEntity:
    return TopologyEntity(
        id=eid,
        kind=kind,
        name=eid,
        status=status,
        ip_addresses=ips or [],
        hostname=hostname or None,
    )


class PowerOnlineTests(unittest.TestCase):
    def test_running_and_paused(self) -> None:
        self.assertTrue(is_power_online("running"))
        self.assertTrue(is_power_online(EntityStatus.RUNNING))
        self.assertTrue(is_power_online("paused"))
        self.assertTrue(is_power_online("online"))

    def test_stopped_unknown_error(self) -> None:
        self.assertFalse(is_power_online("stopped"))
        self.assertFalse(is_power_online("unknown"))
        self.assertFalse(is_power_online(EntityStatus.ERROR))
        self.assertFalse(is_power_online(None))


class SummarizeTests(unittest.TestCase):
    def test_split_and_center(self) -> None:
        ents = [
            _ent("a", status=EntityStatus.RUNNING),
            _ent("b", status=EntityStatus.RUNNING),
            _ent("c", status=EntityStatus.STOPPED),
            _ent("d", status=EntityStatus.UNKNOWN),
        ]
        out = summarize_host_presence(ents)
        self.assertEqual(out["online"], 2)
        self.assertEqual(out["offline"], 2)
        self.assertEqual(out["center"], "2 / 2")
        self.assertEqual(out["unmonitored"], 0)
        self.assertTrue(out["warn"])

    def test_unmonitored_excluded(self) -> None:
        ents = [
            _ent("on", status=EntityStatus.RUNNING),
            _ent("off", status=EntityStatus.STOPPED),
            _ent("skip", status=EntityStatus.STOPPED),
        ]
        out = summarize_host_presence(ents, unmonitored_ids={"skip"})
        self.assertEqual(out["online"], 1)
        self.assertEqual(out["offline"], 1)
        self.assertEqual(out["unmonitored"], 1)
        self.assertEqual(out["total"], 2)

    def test_health_down_flips_running(self) -> None:
        ents = [_ent("web", status=EntityStatus.RUNNING, ips=["192.168.1.10"])]
        out = summarize_host_presence(ents, health_down_ids={"web"})
        self.assertEqual(out["online"], 0)
        self.assertEqual(out["offline"], 1)

    def test_empty(self) -> None:
        out = summarize_host_presence([])
        self.assertEqual(out["online"], 0)
        self.assertEqual(out["offline"], 0)
        self.assertEqual(out["center"], "0 / 0")
        self.assertFalse(out["warn"])


class HealthMatchTests(unittest.TestCase):
    def test_url_host(self) -> None:
        self.assertEqual(check_url_host("https://192.168.1.10/health"), "192.168.1.10")
        self.assertEqual(check_url_host("https://nas.lan:5001"), "nas.lan")
        self.assertEqual(check_url_host(""), "")

    def test_match_ip_only(self) -> None:
        ents = [
            _ent("web", ips=["192.168.1.10"]),
            _ent("db", ips=["192.168.1.20"]),
        ]
        checks = [
            {"url": "https://192.168.1.10/", "last_status": "down", "enabled": True},
            {"url": "https://192.168.1.99/", "last_status": "down", "enabled": True},
        ]
        ids = health_down_entity_ids(ents, checks)
        self.assertEqual(ids, {"web"})

    def test_disabled_or_up_ignored(self) -> None:
        ents = [_ent("web", ips=["10.0.0.5"])]
        checks = [
            {"url": "https://10.0.0.5/", "last_status": "down", "enabled": False},
            {"url": "https://10.0.0.5/", "last_status": "up", "enabled": True},
        ]
        self.assertEqual(health_down_entity_ids(ents, checks), set())


class SplitBagelTests(unittest.TestCase):
    def test_clamp_and_ratio(self) -> None:
        empty = split_bagel_arcs(0, 0, circumference=100)
        self.assertEqual(empty["online_dash"], "0.00 100.00")
        half = split_bagel_arcs(1, 1, circumference=100)
        self.assertEqual(half["online_dash"], "50.00 50.00")
        self.assertEqual(half["offline_dash"], "50.00 50.00")
        self.assertEqual(half["offline_offset"], "-50.00")
        all_on = split_bagel_arcs(3, 0, circumference=100)
        self.assertEqual(all_on["online_dash"], "100.00 0.00")
        self.assertEqual(all_on["offline_dash"], "0.00 100.00")


if __name__ == "__main__":
    unittest.main()
