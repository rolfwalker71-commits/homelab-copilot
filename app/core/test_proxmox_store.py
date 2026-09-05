"""SQLite persist + env fallback for Proxmox hosts (survives process restart)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.core.app_store import AppStore
from app.core.proxmox import (
    endpoints_from_settings,
    hydrate_proxmox_settings,
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


class ProxmoxStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "app.db"
        self.store = AppStore(self.db_path)
        await self.store.connect()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def test_save_then_hydrate_without_env_pve02(self) -> None:
        await self.store.replace_proxmox_hosts(
            [
                {
                    "slot": 1,
                    "host": "192.168.5.101",
                    "port": 8006,
                    "user": "root@pam",
                    "token_id": "copilot",
                    "token_secret": "sec-a",
                    "password": "",
                    "verify_ssl": False,
                    "label": "pve01",
                },
                {
                    "slot": 2,
                    "host": "192.168.5.102",
                    "port": 8006,
                    "user": "root@pam",
                    "token_id": "copilot",
                    "token_secret": "sec-b",
                    "password": "",
                    "verify_ssl": False,
                    "label": "pve02",
                },
            ]
        )

        # New process: no PROXMOX_2_* in env
        fresh = _settings(
            proxmox_host="10.0.0.9",
            proxmox_token_secret="env-only",
        )
        rows = await hydrate_proxmox_settings(fresh, self.store)
        self.assertEqual([r.host for r in rows], ["192.168.5.101", "192.168.5.102"])
        eps = endpoints_from_settings(fresh)
        self.assertEqual([e.id for e in eps], ["primary", "extra:2"])
        self.assertEqual(eps[1].host, "192.168.5.102")
        self.assertEqual(eps[1].token_secret, "sec-b")
        self.assertEqual(fresh.proxmox_2_host, "192.168.5.102")
        self.assertTrue(fresh.proxmox_configured)

    async def test_empty_db_falls_back_to_env(self) -> None:
        s = _settings(
            proxmox_host="192.168.5.101",
            proxmox_token_secret="env-a",
            proxmox_2_host="192.168.5.102",
            proxmox_2_token_secret="env-b",
        )
        rows = await hydrate_proxmox_settings(s, self.store)
        self.assertEqual([r.slot for r in rows], [1, 2])
        self.assertEqual(endpoints_from_settings(s)[1].host, "192.168.5.102")

    async def test_roundtrip_replace_list(self) -> None:
        await self.store.replace_proxmox_hosts(
            [
                {
                    "slot": 2,
                    "host": "192.168.5.102",
                    "token_secret": "only-2",
                    "label": "pve02",
                }
            ]
        )
        listed = await self.store.list_proxmox_hosts()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["slot"], 2)
        self.assertEqual(listed[0]["token_secret"], "only-2")
        self.assertEqual(listed[0]["label"], "pve02")


if __name__ == "__main__":
    unittest.main()
