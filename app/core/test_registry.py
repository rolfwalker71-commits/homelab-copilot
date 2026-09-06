"""Module loader must not split package imports from the started instance."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core.registry import _load_module_file


class RegistryAliasTests(unittest.TestCase):
    def test_load_aliases_package_module_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "demo_mod"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "module.py").write_text(
                "class M:\n"
                "    name = 'demo_mod'\n"
                "    version = '0'\n"
                "    description = ''\n"
                "    def get_router(self):\n"
                "        return None\n"
                "MODULE = M()\n",
                encoding="utf-8",
            )
            inst = _load_module_file(pkg)
            self.assertIsNotNone(inst)
            aliased = sys.modules.get("demo_mod.module")
            loaded = sys.modules.get("homelab_modules.demo_mod")
            self.assertIs(aliased, loaded)
            self.assertIs(inst, loaded.MODULE)
        sys.modules.pop("demo_mod.module", None)
        sys.modules.pop("homelab_modules.demo_mod", None)

    def test_load_reuses_already_imported_package_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "demo_pre"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "module.py").write_text(
                "class M:\n"
                "    name = 'demo_pre'\n"
                "    version = '0'\n"
                "    description = ''\n"
                "    def get_router(self):\n"
                "        return None\n"
                "MODULE = M()\n",
                encoding="utf-8",
            )
            sys.path.insert(0, tmp)
            try:
                import demo_pre.module as pre  # noqa: WPS433

                inst = _load_module_file(pkg)
                self.assertIs(inst, pre.MODULE)
                self.assertIs(sys.modules.get("homelab_modules.demo_pre"), pre)
            finally:
                sys.path.remove(tmp)
                sys.modules.pop("demo_pre.module", None)
                sys.modules.pop("demo_pre", None)
                sys.modules.pop("homelab_modules.demo_pre", None)
