"""Create a Proxmox snapshot before patch apply / release-upgrade."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from app.core.snapshots import auto_snap_name, clamp_keep, guest_can_snapshot
from ops_agent.actor import agent_phrase, by_agent
from patcher.apply import ApplyError
from patcher.config import get_patcher_settings
from patcher.targets import PatchTarget

logger = logging.getLogger(__name__)

LogFn = Callable[[str], Awaitable[None] | None]


async def maybe_pre_snapshot(
    target: PatchTarget,
    *,
    snapshot_first: bool,
    proceed_without_snapshot: bool,
    on_log: LogFn | None = None,
    reason: str = "Patch",
    via_agent: bool = False,
) -> dict[str, Any]:
    """Snapshot a Proxmox VM/LXC before apply. Manual hosts are skipped."""

    async def _log(msg: str) -> None:
        if not on_log:
            return
        result = on_log(msg)
        if hasattr(result, "__await__"):
            await result  # type: ignore[misc]

    if target.kind == "manual" or str(target.id).startswith("manual:"):
        await _log("Kein Proxmox-Guest — Snapshot übersprungen.")
        return {"skipped": True, "reason": "manual"}
    if not guest_can_snapshot(target.id):
        await _log("Ziel ist kein Proxmox-VM/LXC — Snapshot übersprungen.")
        return {"skipped": True, "reason": "not_guest"}
    if not snapshot_first:
        await _log("Snapshot vor Einspielen deaktiviert (Operator).")
        return {"skipped": True, "reason": "disabled"}

    keep = clamp_keep(get_patcher_settings().patcher_snap_keep)
    name = auto_snap_name()
    try:
        from app.main import app as fastapi_app

        engine = getattr(fastapi_app.state, "discovery_engine", None)
        if engine is None:
            raise ApplyError("Discovery-Engine nicht bereit — Snapshot nicht möglich.")
        result = await engine.create_guest_snapshot(
            target.id,
            name=name,
            description=f"HomelabOps vor {reason}",
            prune_keep=keep,
        )
        await _log(
            by_agent(
                f"Proxmox-Snapshot „{name}“ angelegt"
                + (
                    f" (gelöscht: {', '.join(result.get('pruned') or [])})"
                    if result.get("pruned")
                    else ""
                )
                + ".",
                via_agent=via_agent,
            )
        )
        if via_agent:
            await _log(agent_phrase("snap_before") + f" („{name}“).")
        return {"skipped": False, "name": name, **result}
    except PermissionError as exc:
        msg = str(exc)
        if proceed_without_snapshot:
            await _log(f"Snapshot fehlgeschlagen — trotzdem einspielen: {msg}")
            return {"skipped": True, "reason": "acl", "error": msg}
        raise ApplyError(
            f"Snapshot vor {reason} fehlgeschlagen: {msg} "
            "Ohne Snapshot nur mit „trotzdem einspielen“."
        ) from exc
    except Exception as exc:
        msg = getattr(exc, "message", None) or str(exc)
        if proceed_without_snapshot:
            await _log(f"Snapshot fehlgeschlagen — trotzdem einspielen: {msg}")
            return {"skipped": True, "reason": "error", "error": msg}
        raise ApplyError(
            f"Snapshot vor {reason} fehlgeschlagen: {msg} "
            "Ohne Snapshot nur mit „trotzdem einspielen“."
        ) from exc


async def delete_pre_snapshot(
    target_id: str,
    snap_name: str,
    *,
    on_log: LogFn | None = None,
    via_agent: bool = False,
) -> dict[str, Any]:
    """Delete a named Proxmox snap created before apply. Best-effort, never raises."""

    async def _log(msg: str) -> None:
        if not on_log:
            return
        result = on_log(msg)
        if hasattr(result, "__await__"):
            await result  # type: ignore[misc]

    tid = str(target_id or "").strip()
    name = str(snap_name or "").strip()
    if not tid or not name:
        return {"skipped": True, "reason": "missing"}
    if not guest_can_snapshot(tid):
        await _log("Kein Proxmox-Guest — Snapshot-Löschen übersprungen.")
        return {"skipped": True, "reason": "not_guest"}
    try:
        from app.main import app as fastapi_app

        engine = getattr(fastapi_app.state, "discovery_engine", None)
        if engine is None:
            await _log("Discovery-Engine nicht bereit — Snapshot bleibt.")
            return {"skipped": True, "reason": "no_engine"}
        result = await engine.delete_guest_snapshot(tid, name)
        await _log(
            by_agent(
                f"Snapshot nach Erfolg entfernt („{name}“).",
                via_agent=via_agent,
            )
            if via_agent
            else f"Proxmox-Snapshot „{name}“ nach erfolgreichem Image-Update gelöscht."
        )
        return {"skipped": False, "ok": True, "name": name, **(result or {})}
    except Exception as exc:
        msg = getattr(exc, "message", None) or str(exc)
        logger.info("Pre-Image-Snapshot %s nicht gelöscht: %s", name, msg)
        await _log(f"Snapshot „{name}“ nicht gelöscht: {msg}")
        return {"skipped": False, "ok": False, "name": name, "error": msg}


async def rollback_pre_snapshot(
    target_id: str,
    snap_name: str,
    *,
    on_log: LogFn | None = None,
    via_agent: bool = False,
) -> dict[str, Any]:
    """Rollback to the named pre-apply snap via the existing guest API."""

    async def _log(msg: str) -> None:
        if not on_log:
            return
        result = on_log(msg)
        if hasattr(result, "__await__"):
            await result  # type: ignore[misc]

    tid = str(target_id or "").strip()
    name = str(snap_name or "").strip()
    if not tid or not name:
        return {"skipped": True, "ok": False, "reason": "missing"}
    if not guest_can_snapshot(tid):
        await _log("Kein Proxmox-Guest — Rollback übersprungen.")
        return {"skipped": True, "ok": False, "reason": "not_guest"}
    try:
        from app.main import app as fastapi_app

        engine = getattr(fastapi_app.state, "discovery_engine", None)
        if engine is None:
            await _log("Discovery-Engine nicht bereit — Rollback nicht möglich.")
            return {
                "skipped": False,
                "ok": False,
                "name": name,
                "error": "Discovery-Engine nicht bereit.",
            }
        result = await engine.rollback_guest_snapshot(tid, name)
        await _log(agent_phrase("rolled_back", via_agent=via_agent, snap=name))
        return {"skipped": False, "ok": True, "name": name, **(result or {})}
    except Exception as exc:
        msg = getattr(exc, "message", None) or str(exc)
        logger.info("Pre-Apply-Rollback %s fehlgeschlagen: %s", name, msg)
        await _log(f"Rollback auf „{name}“ fehlgeschlagen: {msg}")
        return {"skipped": False, "ok": False, "name": name, "error": msg}
