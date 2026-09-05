"""Reconcile live PVE guests against the cached topology (Hosts rail)."""

from __future__ import annotations

import unittest

from app.core.models import EntityKind, EntityStatus, TopologyEntity, TopologySnapshot
from app.core.reconcile import guest_source_label, is_live_rail_guest, reconcile_topology
from app.core.tree import build_topology_tree


def _guest(
    name: str,
    vmid: int,
    *,
    node: str = "pve01",
    status: EntityStatus = EntityStatus.RUNNING,
    ips: list[str] | None = None,
    kind: EntityKind = EntityKind.LXC,
    meta: dict | None = None,
) -> TopologyEntity:
    payload = {"pve_source": node}
    if meta:
        payload.update(meta)
    return TopologyEntity(
        id=f"{kind.value}:{node}:{vmid}",
        kind=kind,
        name=name,
        status=status,
        node=node,
        vmid=vmid,
        hostname=name,
        ip_addresses=ips or [],
        parent_id=f"node:{node}",
        meta=payload,
    )


def _node(name: str = "pve01") -> TopologyEntity:
    return TopologyEntity(
        id=f"node:{name}",
        kind=EntityKind.NODE,
        name=name,
        status=EntityStatus.RUNNING,
        node=name,
        hostname=name,
    )


def _manual(hid: int, name: str, ip: str) -> TopologyEntity:
    return TopologyEntity(
        id=f"manual:{hid}",
        kind=EntityKind.HOST,
        name=name,
        status=EntityStatus.RUNNING,
        hostname=name,
        ip_addresses=[ip],
        meta={"source": "manual"},
    )


def _snap(
    *,
    guests: list[TopologyEntity] | None = None,
    nodes: list[TopologyEntity] | None = None,
    hosts: list[TopologyEntity] | None = None,
    errors: list[str] | None = None,
    proxmox_configured: bool = True,
) -> TopologySnapshot:
    return TopologySnapshot(
        refreshed_at="jetzt",
        refreshed_at_iso="2026-09-05T19:00:00Z",
        nodes=nodes if nodes is not None else [_node()],
        guests=guests or [],
        hosts=hosts or [],
        errors=errors or [],
        proxmox_configured=proxmox_configured,
    )


class ReconcileMergeTests(unittest.TestCase):
    def test_name_same_vmid_changed(self) -> None:
        prev = _snap(
            guests=[
                _guest("stirlingpdf", 104, ips=["192.168.5.104"]),
                _guest("rustdesk", 116, status=EntityStatus.RUNNING),
            ]
        )
        live = _snap(
            guests=[
                _guest("stirlingpdf", 108, ips=["192.168.5.108"]),
                _guest("rustdesk", 116, status=EntityStatus.STOPPED),
            ]
        )
        out, stats = reconcile_topology(prev, live)
        ids = {g.id: g for g in out.guests}
        self.assertIn("lxc:pve01:108", ids)
        self.assertNotIn("lxc:pve01:104", ids)
        self.assertEqual(ids["lxc:pve01:108"].vmid, 108)
        self.assertEqual(ids["lxc:pve01:108"].ip_addresses, ["192.168.5.108"])
        self.assertEqual(ids["lxc:pve01:116"].status, EntityStatus.STOPPED)
        self.assertEqual(stats.updated, 2)
        self.assertEqual(stats.removed, 0)
        self.assertEqual(stats.added, 0)
        self.assertIn(("lxc:pve01:104", "lxc:pve01:108"), stats.id_changes)
        self.assertIn("2 aktualisiert", stats.message_de())

    def test_vanished_vmid_dropped_from_rail(self) -> None:
        prev = _snap(
            guests=[
                _guest("nginxproxymanager", 103),
                _guest("lxc-114", 114, status=EntityStatus.UNKNOWN),
                _guest("lxc-118", 118, status=EntityStatus.UNKNOWN),
            ]
        )
        live = _snap(guests=[_guest("nginxproxymanager", 103)])
        out, stats = reconcile_topology(prev, live)
        names = [g.name for g in out.guests]
        self.assertEqual(names, ["nginxproxymanager"])
        self.assertEqual(stats.removed, 2)
        self.assertEqual(stats.unchanged, 1)
        self.assertCountEqual(stats.removed_ids, ["lxc:pve01:114", "lxc:pve01:118"])
        tree = build_topology_tree(out)
        rail_names = [row["guest"].name for row in tree["nodes"][0]["guests"]]
        self.assertEqual(rail_names, ["nginxproxymanager"])
        self.assertNotIn("lxc-114", rail_names)
        self.assertIn("2 Gäste entfernt", stats.message_de())

    def test_manual_linux_hosts_kept(self) -> None:
        manual = _manual(3, "nas", "192.168.5.20")
        prev = _snap(guests=[_guest("gone", 114)], hosts=[manual])
        live = _snap(guests=[], hosts=[manual])
        out, stats = reconcile_topology(prev, live)
        self.assertEqual([h.id for h in out.hosts], ["manual:3"])
        self.assertEqual(stats.removed, 1)
        self.assertEqual(len(out.guests), 0)

    def test_no_ghost_node_from_leftover_guest(self) -> None:
        prev = _snap(
            nodes=[_node("pve01")],
            guests=[_guest("ghost", 199, node="pve-gone")],
        )
        live = _snap(
            nodes=[_node("pve01")],
            guests=[
                _guest("nginxproxymanager", 103),
                _guest("ghost", 199, node="pve-gone"),
            ],
        )
        out, stats = reconcile_topology(prev, live)
        self.assertEqual([g.node for g in out.guests], ["pve01"])
        self.assertEqual(stats.removed, 1)
        tree = build_topology_tree(out)
        self.assertEqual([n["name"] for n in tree["nodes"]], ["pve01"])

    def test_pve_hard_fail_keeps_previous_guests(self) -> None:
        prev = _snap(guests=[_guest("vaultwarden", 113)])
        live = _snap(
            nodes=[],
            guests=[],
            errors=["Proxmox-Discovery fehlgeschlagen: ConnectError"],
        )
        out, stats = reconcile_topology(prev, live)
        self.assertEqual([g.vmid for g in out.guests], [113])
        self.assertTrue(stats.pve_kept_previous)
        self.assertIn("unverändert", stats.message_de())

    def test_same_vmid_keeps_ip_when_live_empty(self) -> None:
        prev = _snap(guests=[_guest("rustdesk", 116, ips=["192.168.5.116"])])
        live = _snap(
            guests=[_guest("rustdesk", 116, status=EntityStatus.STOPPED, ips=[])]
        )
        out, _stats = reconcile_topology(prev, live)
        g = out.guests[0]
        self.assertEqual(g.status, EntityStatus.STOPPED)
        self.assertEqual(g.ip_addresses, ["192.168.5.116"])


class TreeRailTests(unittest.TestCase):
    def test_tree_does_not_invent_node_from_guest(self) -> None:
        snap = _snap(
            nodes=[_node("pve01")],
            guests=[
                _guest("ok", 103),
                _guest("stray", 1, node="missing-node"),
            ],
        )
        tree = build_topology_tree(snap)
        self.assertEqual([n["name"] for n in tree["nodes"]], ["pve01"])
        self.assertEqual([row["guest"].name for row in tree["nodes"][0]["guests"]], ["ok"])


class LiveRailFilterTests(unittest.TestCase):
    def test_unknown_nameless_resource_not_on_rail(self) -> None:
        """Do not keep invented ``lxc-114`` when status is unknown (no config)."""
        prev = _snap(guests=[_guest("nginxproxymanager", 103)])
        live = _snap(
            guests=[
                _guest("nginxproxymanager", 103),
                _guest("lxc-114", 114, status=EntityStatus.UNKNOWN),
                _guest("lxc-118", 118, status=EntityStatus.UNKNOWN, node="pve02"),
                _guest("lxc-150", 150, status=EntityStatus.UNKNOWN),
            ]
        )
        out, stats = reconcile_topology(prev, live)
        names = [g.name for g in out.guests]
        self.assertEqual(names, ["nginxproxymanager"])
        self.assertNotIn("lxc-114", names)
        self.assertEqual(stats.added, 0)
        tree = build_topology_tree(out)
        rail_names = [row["guest"].name for row in tree["nodes"][0]["guests"]]
        self.assertEqual(rail_names, ["nginxproxymanager"])

    def test_vanished_from_all_apis_gone(self) -> None:
        prev = _snap(
            guests=[
                _guest("vaultwarden", 113),
                _guest("lxc-114", 114, status=EntityStatus.UNKNOWN),
            ]
        )
        live = _snap(guests=[_guest("vaultwarden", 113)])
        out, stats = reconcile_topology(prev, live)
        self.assertEqual([g.name for g in out.guests], ["vaultwarden"])
        self.assertEqual(stats.removed, 1)
        self.assertEqual(stats.removed_ids, ("lxc:pve01:114",))
        tree = build_topology_tree(out)
        rail = [row["guest"].name for row in tree["nodes"][0]["guests"]]
        self.assertNotIn("lxc-114", rail)

    def test_template_dropped_from_rail(self) -> None:
        live = _snap(
            guests=[
                _guest("ok", 103),
                _guest("tmpl", 900, meta={"template": True}),
            ]
        )
        out, _stats = reconcile_topology(None, live)
        self.assertEqual([g.name for g in out.guests], ["ok"])
        self.assertFalse(is_live_rail_guest(live.guests[1]))

    def test_added_toast_includes_pve_source(self) -> None:
        prev = _snap(guests=[_guest("vaultwarden", 113)])
        live = _snap(
            nodes=[_node("pve01"), _node("pve02")],
            guests=[
                _guest("vaultwarden", 113),
                _guest("paperless", 105, node="pve02", meta={"pve_source": "pve02"}),
            ]
        )
        _out, stats = reconcile_topology(prev, live)
        self.assertEqual(stats.added, 1)
        self.assertEqual(stats.added_from, (("paperless", "pve02"),))
        self.assertIn("paperless←pve02", stats.message_de())
        self.assertEqual(guest_source_label(live.guests[1]), "pve02")
