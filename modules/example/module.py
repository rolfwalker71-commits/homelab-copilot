"""Example drop-in module — proves the registry contract.

Copy this pattern into ``patcher/`` or ``backup_verifier/`` when those
features are implemented. Filename must be ``module.py`` and export ``MODULE``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, FastAPI

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/ping")
async def ping() -> dict[str, str]:
    return {"module": "example", "status": "ok"}


class ExampleModule:
    name = "example"
    version = "0.1.0"
    description = "Demonstrationsmodul für das Plugin-Framework (Phase 1)."
    enabled = True
    meta = {"phase": 1, "role": "demo"}

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app: FastAPI) -> None:
        logger.info("Example module started")

    async def on_shutdown(self, app: FastAPI) -> None:
        logger.info("Example module stopped")

    async def on_topology_refresh(self, topology: dict[str, Any]) -> None:
        # Future modules (patcher / backup_verifier) hook here.
        summary = {
            "nodes": len(topology.get("nodes") or []),
            "guests": len(topology.get("guests") or []),
            "containers": len(topology.get("containers") or []),
        }
        logger.debug("Example module saw topology refresh: %s", summary)


MODULE = ExampleModule()
