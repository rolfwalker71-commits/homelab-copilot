"""Modular extension / plugin registry.

Future modules (e.g. ``modules/patcher``, ``modules/backup_verifier``) register
themselves here without touching core discovery or the FastAPI bootstrap beyond
a single ``discover_and_load_modules()`` call.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from fastapi import APIRouter, FastAPI

logger = logging.getLogger(__name__)


class ModuleProtocol(Protocol):
    """Contract every drop-in module must satisfy."""

    name: str
    version: str
    description: str

    def get_router(self) -> APIRouter | None:
        """Optional API router mounted under ``/api/modules/{name}``."""
        ...

    async def on_startup(self, app: FastAPI) -> None:
        """Called once after the FastAPI app is ready."""
        ...

    async def on_shutdown(self, app: FastAPI) -> None:
        """Called during application shutdown."""
        ...

    async def on_topology_refresh(self, topology: dict[str, Any]) -> None:
        """Hook after each discovery refresh — ideal for patch/backup modules."""
        ...


StartupHook = Callable[[FastAPI], Awaitable[None]]
TopologyHook = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class RegisteredModule:
    name: str
    version: str
    description: str
    instance: Any
    router: APIRouter | None = None
    enabled: bool = True
    meta: dict[str, Any] = field(default_factory=dict)


class ModuleRegistry:
    """In-process registry of loaded feature modules."""

    def __init__(self) -> None:
        self._modules: dict[str, RegisteredModule] = {}

    def register(self, module: Any) -> RegisteredModule:
        name = getattr(module, "name", None)
        if not name:
            raise ValueError("Module must expose a non-empty ``name`` attribute")
        if name in self._modules:
            raise ValueError(f"Module already registered: {name}")

        router = module.get_router() if hasattr(module, "get_router") else None
        entry = RegisteredModule(
            name=name,
            version=getattr(module, "version", "0.0.0"),
            description=getattr(module, "description", ""),
            instance=module,
            router=router,
            enabled=getattr(module, "enabled", True),
            meta=getattr(module, "meta", {}) or {},
        )
        self._modules[name] = entry
        logger.info("Registered module '%s' v%s", entry.name, entry.version)
        return entry

    def list_modules(self) -> list[RegisteredModule]:
        return list(self._modules.values())

    def get(self, name: str) -> RegisteredModule | None:
        return self._modules.get(name)

    def mount_routers(self, app: FastAPI) -> None:
        for entry in self._modules.values():
            if not entry.enabled or entry.router is None:
                continue
            prefix = f"/api/modules/{entry.name}"
            app.include_router(entry.router, prefix=prefix, tags=[f"module:{entry.name}"])
            logger.info("Mounted module router at %s", prefix)

    async def run_startup(self, app: FastAPI) -> None:
        for entry in self._modules.values():
            if not entry.enabled:
                continue
            hook = getattr(entry.instance, "on_startup", None)
            if hook:
                await hook(app)

    async def run_shutdown(self, app: FastAPI) -> None:
        for entry in self._modules.values():
            hook = getattr(entry.instance, "on_shutdown", None)
            if hook:
                try:
                    await hook(app)
                except Exception:
                    logger.exception("Module '%s' shutdown failed", entry.name)

    async def notify_topology_refresh(self, topology: dict[str, Any]) -> None:
        for entry in self._modules.values():
            if not entry.enabled:
                continue
            hook = getattr(entry.instance, "on_topology_refresh", None)
            if not hook:
                continue
            try:
                await hook(topology)
            except Exception:
                logger.exception("Module '%s' topology hook failed", entry.name)


registry = ModuleRegistry()


def _load_module_file(path: Path) -> Any | None:
    """Import ``modules/<name>/module.py`` if present."""
    module_py = path / "module.py"
    if not module_py.is_file():
        return None
    mod_name = f"homelab_modules.{path.name}"
    spec = importlib.util.spec_from_file_location(mod_name, module_py)
    if spec is None or spec.loader is None:
        return None
    # Registry loads as homelab_modules.<name>, but other code does
    # `from patcher.module import …`. Keep one instance so `_store` set in
    # on_startup is the same object Scan jetzt / scan-all use.
    pkg_mod = f"{path.name}.module"
    existing = sys.modules.get(pkg_mod)
    if existing is not None:
        sys.modules[mod_name] = existing
        return getattr(existing, "MODULE", None) or getattr(existing, "module", None)

    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    sys.modules[pkg_mod] = mod
    pkg = sys.modules.get(path.name)
    if pkg is not None:
        setattr(pkg, "module", mod)
    spec.loader.exec_module(mod)
    return getattr(mod, "MODULE", None) or getattr(mod, "module", None)


def discover_and_load_modules(modules_dir: Path) -> ModuleRegistry:
    """Scan ``modules_dir`` for drop-in packages with a ``module.py`` entrypoint.

    Placeholder dirs (``patcher/``, ``backup_verifier/``) without ``module.py``
    are silently skipped until implemented.
    """
    if not modules_dir.is_dir():
        logger.warning("Modules directory missing: %s", modules_dir)
        return registry

    for child in sorted(modules_dir.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        try:
            instance = _load_module_file(child)
            if instance is None:
                logger.debug("No module.py in %s — skipping (placeholder OK)", child.name)
                continue
            registry.register(instance)
        except Exception:
            logger.exception("Failed to load module from %s", child)

    return registry
