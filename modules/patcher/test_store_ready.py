"""Scan-all / Scan jetzt must use the same store as Patcher startup."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
_MODULES = _ROOT / "modules"
for _p in (_ROOT, _MODULES):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


class StoreResolveTests(unittest.TestCase):
    def test_get_store_binds_app_state_when_module_store_is_none(self) -> None:
        import patcher.module as pm

        sentinel = SimpleNamespace(name="state-store")
        prev = pm._store
        try:
            pm._store = None
            with patch.object(pm, "_store_from_app_state", return_value=sentinel):
                self.assertIs(pm._get_store(), sentinel)
                self.assertIs(pm._store, sentinel)
        finally:
            pm._store = prev

    def test_get_store_raises_german_when_unbound(self) -> None:
        import patcher.module as pm
        from fastapi import HTTPException

        prev = pm._store
        try:
            pm._store = None
            with patch.object(pm, "_store_from_app_state", return_value=None):
                with self.assertRaises(HTTPException) as ctx:
                    pm._get_store()
            self.assertEqual(ctx.exception.status_code, 503)
            self.assertEqual(ctx.exception.detail, "Patcher-Store nicht bereit.")
        finally:
            pm._store = prev


class EnsureStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_store_opens_data_dir_db(self) -> None:
        import patcher.module as pm

        prev = pm._store
        with tempfile.TemporaryDirectory() as tmp:
            fake_ps = SimpleNamespace(db_path=Path(tmp) / "patcher.db")
            try:
                pm._store = None
                with patch.object(pm, "_store_from_app_state", return_value=None):
                    with patch.object(pm, "get_patcher_settings", return_value=fake_ps):
                        store = await pm._ensure_store()
                self.assertTrue((Path(tmp) / "patcher.db").exists())
                self.assertIs(pm._store, store)
                await store.close()
            finally:
                pm._store = prev

    def test_bind_store_syncs_module_aliases(self) -> None:
        import patcher.module as pm

        sentinel = SimpleNamespace(name="shared-store")
        alias = types.ModuleType("homelab_modules.patcher")
        alias._store = None
        prev = pm._store
        prev_alias = sys.modules.get("homelab_modules.patcher")
        try:
            sys.modules["homelab_modules.patcher"] = alias
            pm._store = None
            pm._bind_store(sentinel)
            self.assertIs(pm._store, sentinel)
            self.assertIs(alias._store, sentinel)
        finally:
            pm._store = prev
            if prev_alias is None:
                sys.modules.pop("homelab_modules.patcher", None)
            else:
                sys.modules["homelab_modules.patcher"] = prev_alias

    async def test_ensure_store_reuses_app_state_not_new_db(self) -> None:
        import patcher.module as pm

        sentinel = SimpleNamespace(name="already-open")
        prev = pm._store
        try:
            pm._store = None
            with patch.object(pm, "_store_from_app_state", return_value=sentinel):
                with patch.object(pm, "get_patcher_settings") as settings:
                    store = await pm._ensure_store()
            self.assertIs(store, sentinel)
            settings.assert_not_called()
        finally:
            pm._store = prev


class ScanAllUsesEnsureTests(unittest.TestCase):
    def test_run_scan_all_opens_store(self) -> None:
        src = Path(__file__).resolve().with_name("module.py").read_text(encoding="utf-8")
        start = src.find("async def _run_scan_all")
        nxt = src.find("\nasync def ", start + 1)
        body = src[start:nxt]
        self.assertIn("await _ensure_store(app)", body)
        self.assertNotIn("store = _store", body)
