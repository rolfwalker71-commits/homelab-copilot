"""AI-Driven Patch-Management — ModuleProtocol entrypoint."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

_MODULES_ROOT = Path(__file__).resolve().parent.parent
if str(_MODULES_ROOT) not in sys.path:
    sys.path.insert(0, str(_MODULES_ROOT))

from app.config import get_settings
from app.core.docker_control import (
    DockerControlError,
    apply_image_updates,
    scan_image_updates,
    ssh_key_present,
)
from app.core.locale import format_de, now_berlin
from app.core.topology import TopologyStore

from patcher.agent import WaveEngine, hosts_from_store
from patcher.apply import ApplyError, apply_updates, reboot_host
from patcher.pre_snap import maybe_pre_snapshot
from patcher.release_upgrade import perform_release_upgrade
from patcher.config import get_patcher_settings
from patcher import cron as cron_mod
from patcher.explain import explain_apply_run, explain_patch_job
from ops_agent.actor import agent_phrase, by_agent
from patcher.jobs import JOBS
from patcher.llm import LlmError, summarize_scan
from patcher.scan import ScanError, scan_target
from patcher.scheduler import SCAN_ALL, begin_scan_all, daily_scan_loop, run_scan_all_hosts
from patcher.sshutil import ssh_probe
from patcher.store import PatcherStore
from patcher.targets import list_targets, resolve_target

logger = logging.getLogger(__name__)

router = APIRouter()
_store: PatcherStore | None = None
_daily_task: Any = None
_engine: WaveEngine | None = None


def _make_templates() -> Jinja2Templates:
    module_dir = Path(__file__).resolve().parent / "templates"
    app_dir = Path(__file__).resolve().parents[2] / "app" / "templates"
    env = Environment(
        loader=ChoiceLoader(
            [
                FileSystemLoader(str(module_dir)),
                FileSystemLoader(str(app_dir)),
            ]
        ),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.globals["format_de"] = format_de
    templates = Jinja2Templates(directory=str(module_dir))
    templates.env = env
    return templates


def _get_store() -> PatcherStore:
    if _store is None:
        raise HTTPException(status_code=503, detail="Patcher-Store nicht bereit.")
    return _store


def _get_engine() -> WaveEngine:
    if _engine is None:
        raise HTTPException(status_code=503, detail="Wellen-Agent nicht bereit.")
    return _engine


def _agent_http(exc: Exception) -> HTTPException:
    msg = getattr(exc, "message", None) or str(exc)
    return HTTPException(status_code=400, detail=msg)


async def _inventory_tags(target_id: str) -> list[str]:
    try:
        from app.main import app as fastapi_app

        inv = getattr(fastapi_app.state, "inventory_store", None)
        if inv is None:
            return []
        row = await inv.get(target_id)
        return [str(t) for t in (row.get("extra_tags") or []) if t]
    except Exception:
        return []


async def _job_payload(job) -> dict[str, Any]:
    data = job.to_dict()
    wave_item = None
    store = _store
    if store is not None:
        try:
            wave_item = await store.get_wave_item_by_job(job.id)
        except Exception:
            wave_item = None
    if wave_item:
        data["wave_item"] = wave_item
        data["explanation"] = wave_item.get("explanation") or explain_patch_job(data)
    else:
        data["explanation"] = explain_patch_job(data)
    return data


async def _maybe_plan_after_scan(snapshot) -> None:
    engine = _engine
    if engine is not None:
        try:
            policy = await engine.policy()
            if policy.enabled:
                hosts = await hosts_from_store(
                    engine.store, snapshot, tags_for=_inventory_tags
                )
                if hosts:
                    await engine.plan(hosts)
        except RuntimeError as exc:
            logger.info("Welle nach Scan nicht geplant: %s", exc)
        except Exception:
            logger.exception("Welle nach Scan nicht geplant")
    try:
        from app.main import app as fastapi_app

        ops = getattr(fastapi_app.state, "ops_engine", None)
        if ops is not None:
            await ops.propose(auto_apply=True)
    except Exception:
        logger.exception("Ops-Fenster nach Scan nicht geplant")


def _snapshot(request: Request):
    store: TopologyStore = request.app.state.topology_store
    return store.snapshot


def _http_docker(exc: DockerControlError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


# --- payloads ---


class ScanPayload(BaseModel):
    target_id: str = Field(..., min_length=1)
    wait: bool = False
    summarize: bool = True


class ApplyPayload(BaseModel):
    target_id: str = Field(..., min_length=1)
    confirm: bool = False
    package_filter: str = Field(default="all", pattern="^(security|all|selected)$")
    packages: list[str] = Field(default_factory=list)
    reboot_after: bool = False
    snapshot_first: bool = True
    proceed_without_snapshot: bool = False
    wait: bool = False


class ApplyBatchPayload(BaseModel):
    target_ids: list[str] = Field(..., min_length=1)
    confirm: bool = False
    package_filter: str = Field(default="all", pattern="^(security|all)$")
    reboot_after: bool = False
    snapshot_first: bool = True
    proceed_without_snapshot: bool = False
    wait: bool = False


class RebootPayload(BaseModel):
    target_id: str = Field(..., min_length=1)
    confirm: bool = False


class HostPayload(BaseModel):
    id: int | None = None
    name: str = Field(..., min_length=1, max_length=128)
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, max_length=64)
    enabled: bool = True
    note: str = Field(default="", max_length=500)


class HostCheckPayload(BaseModel):
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, max_length=64)


class SchedulePayload(BaseModel):
    id: int | None = None
    target_id: str = Field(..., min_length=1)
    preset: str = Field(default="daily", pattern="^(daily|weekly|custom)$")
    time: str = Field(default="04:00")
    weekday: int = Field(default=0, ge=0, le=6)
    cron_expr: str | None = None
    enabled: bool = True
    note: str = ""


class SummarizePayload(BaseModel):
    scan_id: int


class MonitorPayload(BaseModel):
    target_id: str = Field(..., min_length=1)
    monitored: bool


class ImageScanPayload(BaseModel):
    target_id: str = Field(..., min_length=1)
    wait: bool = False


class ImageApplyPayload(BaseModel):
    target_id: str = Field(..., min_length=1)
    confirm: bool = False
    names: list[str] = Field(default_factory=list)
    restart: bool = True
    prune: bool = True
    wait: bool = False
    snapshot_first: bool = True
    proceed_without_snapshot: bool = False


class ReleaseUpgradePayload(BaseModel):
    target_id: str = Field(..., min_length=1)
    confirm: bool = False
    reboot_after: bool = False
    snapshot_first: bool = True
    proceed_without_snapshot: bool = False
    wait: bool = False


class AgentSettingsPayload(BaseModel):
    enabled: bool | None = None
    auto_security: bool | None = None
    max_parallel: int | None = Field(default=None, ge=1, le=8)


class AgentConfirmPayload(BaseModel):
    item_ids: list[int] = Field(default_factory=list)
    all_waiting: bool = False


# --- helpers ---


async def _enrich_targets(store: PatcherStore, snapshot) -> list[dict[str, Any]]:
    targets = await list_targets(store, snapshot)
    unmonitored = await store.list_unmonitored_ids()
    out: list[dict[str, Any]] = []
    for t in targets:
        d = t.to_dict()
        d["monitored"] = t.id not in unmonitored
        latest = await store.latest_scan_for_target(t.id)
        if latest and latest.get("status") == "success":
            scan_sum = latest.get("summary") or {}
            ru = scan_sum.get("release_upgrade")
            if not isinstance(ru, dict) or not ru.get("available"):
                ru = None
            d["last_scan"] = {
                "id": latest["id"],
                "created_at": latest.get("created_at"),
                "pm": latest.get("pm"),
                "distro": latest.get("distro"),
                "summary": scan_sum,
                "llm_summary": latest.get("llm_summary"),
                "reboot_required": latest.get("reboot_required"),
                "release_upgrade": ru,
            }
            d["pending"] = scan_sum.get("total", 0)
            d["security"] = scan_sum.get("security", 0)
            d["release_upgrade"] = ru
        else:
            d["last_scan"] = None
            d["pending"] = None
            d["security"] = None
            d["release_upgrade"] = None
            if latest:
                d["last_scan_error"] = latest.get("error_message")
        img = await store.latest_image_scan_for_target(t.id, success_only=True)
        if img and img.get("status") == "success":
            d["image_updates"] = int(img.get("update_count") or 0)
            d["last_image_scan"] = {
                "id": img["id"],
                "created_at": img.get("created_at"),
                "count": int(img.get("update_count") or 0),
                "summary": img.get("summary") or {},
            }
        else:
            d["image_updates"] = None
            d["last_image_scan"] = None
        out.append(d)
    return out


def _compact_image_summary(result: dict[str, Any]) -> dict[str, Any]:
    updates = []
    for u in result.get("updates") or []:
        if not isinstance(u, dict):
            continue
        updates.append(
            {
                "name": u.get("name"),
                "image": u.get("image"),
                "stack": u.get("stack") or "",
            }
        )
    return {
        "count": int(result.get("count") or 0),
        "message": result.get("message"),
        "updates": updates,
    }


async def _scan_and_persist_images(target, snapshot) -> dict[str, Any]:
    """Scan Docker images on a host and persist the last result (daily / card)."""
    store = _store
    if store is None:
        return {"ok": False, "count": 0, "error": "Store nicht bereit"}
    scan_id = await store.create_image_scan(
        target_id=target.id, target_name=target.name, status="running"
    )
    try:
        result = await scan_image_updates(
            get_settings(),
            parent_id=target.id,
            snapshot=snapshot,
        )
        count = int(result.get("count") or 0)
        await store.finish_image_scan(
            scan_id,
            status="success",
            update_count=count,
            summary=_compact_image_summary(result),
        )
        return result
    except (DockerControlError, Exception) as exc:
        msg = getattr(exc, "message", None) or str(exc)
        logger.info("image scan %s: %s", getattr(target, "id", "?"), msg)
        try:
            await store.finish_image_scan(
                scan_id, status="failed", error_message=msg
            )
        except Exception:
            logger.exception("finish_image_scan failed")
        return {"ok": False, "count": 0, "error": msg, "updates": []}


async def _run_image_scan_job(
    job_id: str,
    *,
    target_id: str,
    snapshot,
) -> None:
    store = _store
    if store is None:
        JOBS.finish(job_id, status="failed", error="Store nicht bereit")
        return
    try:
        target = await resolve_target(store, snapshot, target_id)
        JOBS.set_progress(
            job_id,
            phase="Images",
            percent=15,
            message=f"Prüfe Docker-Images auf {target.name}…",
        )
        JOBS.append_log(job_id, f"Image-Scan auf {target.name} ({target.ip})")
        JOBS.append_log(
            job_id,
            "Prüfung ohne Pull — vergleiche lokale Digests mit dem Registry-Manifest.",
        )
        result = await _scan_and_persist_images(target, snapshot)
        if result.get("error") and not result.get("ok", True):
            JOBS.finish(
                job_id,
                status="failed",
                error=str(result.get("error")),
                result=result,
            )
            return
        count = int(result.get("count") or 0)
        for u in result.get("updates") or []:
            if isinstance(u, dict):
                JOBS.append_log(
                    job_id,
                    f"{(u.get('stack') + '/') if u.get('stack') else ''}"
                    f"{u.get('name')} · {u.get('image') or ''}",
                )
        for c in result.get("containers") or []:
            if isinstance(c, dict) and c.get("error"):
                JOBS.append_log(
                    job_id,
                    f"{c.get('name')}: {c.get('error')}",
                )
        JOBS.finish(
            job_id,
            status="success",
            result={
                "count": count,
                "updates": result.get("updates") or [],
                "message": result.get("message"),
            },
            message=result.get("message") or f"{count} Image-Update(s)",
            phase="Fertig",
        )
    except (DockerControlError, RuntimeError) as exc:
        msg = getattr(exc, "message", None) or str(exc)
        JOBS.finish(job_id, status="failed", error=msg)
    except Exception as exc:
        logger.exception("image-scan job failed")
        JOBS.finish(job_id, status="failed", error=str(exc))


def _job_via_agent(job_id: str) -> bool:
    job = JOBS.get(job_id)
    return bool(job and getattr(job, "via_agent", False))


def _fail_result(snap_info: dict[str, Any] | None, *, via_agent: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"via_agent": via_agent}
    if isinstance(snap_info, dict) and not snap_info.get("skipped") and snap_info.get("name"):
        out["snapshot"] = snap_info
    return out


async def _run_image_apply_job(
    job_id: str,
    *,
    target_id: str,
    snapshot,
    names: list[str],
    restart: bool,
    prune: bool = True,
    snapshot_first: bool = True,
    proceed_without_snapshot: bool = False,
) -> None:
    store = _store
    if store is None:
        JOBS.finish(job_id, status="failed", error="Store nicht bereit")
        return
    via_agent = _job_via_agent(job_id)
    snap_info: dict[str, Any] | None = None
    try:
        target = await resolve_target(store, snapshot, target_id)
        JOBS.set_progress(
            job_id,
            phase="Snapshot",
            percent=6,
            message=f"Snapshot vor Image-Update auf {target.name}…",
        )

        async def on_snap_log(line: str) -> None:
            JOBS.append_log(job_id, line)

        snap_info = await maybe_pre_snapshot(
            target,
            snapshot_first=snapshot_first,
            proceed_without_snapshot=proceed_without_snapshot,
            on_log=on_snap_log,
            reason="Image-Update",
            via_agent=via_agent,
        )
        JOBS.set_progress(
            job_id,
            phase="Pull",
            percent=10,
            message=f"Hole Images auf {target.name}…",
        )
        JOBS.append_log(
            job_id,
            f"Image-Update auf {target.name}: {len(names)} Container, "
            f"{'mit Neustart' if restart else 'ohne Neustart'}",
        )

        async def on_progress(phase: str, percent: int, message: str) -> None:
            JOBS.set_progress(job_id, phase=phase, percent=percent, message=message)

        line_n = 0

        async def on_line(line: str) -> None:
            nonlocal line_n
            line_n += 1
            JOBS.append_log(job_id, line)
            if "Ungenutzte Images" in line:
                await on_progress("Bereinigen", 88, line[:200])
                return
            if line_n == 1 or line_n % 4 == 0:
                await on_progress(
                    "Pull",
                    min(85, 15 + min(60, line_n)),
                    line[:200],
                )

        result = await apply_image_updates(
            get_settings(),
            parent_id=target.id,
            snapshot=snapshot,
            names=names,
            restart=restart,
            prune=prune,
            on_line=on_line,
        )
        if isinstance(result, dict):
            result = {**result, "snapshot": snap_info, "via_agent": via_agent}
        JOBS.set_progress(
            job_id, phase="Prüfen", percent=90, message="Zähle verbleibende Image-Updates…"
        )
        refreshed = await _scan_and_persist_images(target, snapshot)
        if refreshed.get("error") and store is not None:
            sid = await store.create_image_scan(
                target_id=target.id, target_name=target.name
            )
            await store.finish_image_scan(
                sid,
                status="success",
                update_count=0,
                summary={
                    "count": 0,
                    "message": result.get("message") or "Images aktualisiert.",
                    "updates": [],
                },
            )
        remaining = int(refreshed.get("count") or 0)
        done_msg = result.get("message") or "Images aktualisiert."
        if remaining:
            done_msg = f"{done_msg} Noch {remaining} Image-Update(s)."
        else:
            done_msg = f"{done_msg} Keine weiteren Image-Updates."
        if via_agent:
            done_msg = (
                agent_phrase("images_applied")
                if done_msg.startswith("Images aktualisiert")
                else by_agent(done_msg)
            )
        JOBS.finish(
            job_id,
            status="success",
            result=result,
            message=done_msg,
            phase="Fertig",
        )
    except (DockerControlError, RuntimeError) as exc:
        msg = getattr(exc, "message", None) or str(exc)
        JOBS.finish(
            job_id,
            status="failed",
            error=msg,
            result=_fail_result(snap_info, via_agent=via_agent),
        )
    except Exception as exc:
        logger.exception("image-apply job failed")
        JOBS.finish(
            job_id,
            status="failed",
            error=str(exc),
            result=_fail_result(snap_info, via_agent=via_agent),
        )


async def _run_scan_job(
    job_id: str,
    *,
    target_id: str,
    snapshot,
    do_summarize: bool,
) -> None:
    store = _store
    if store is None:
        JOBS.finish(job_id, status="failed", error="Store nicht bereit")
        return {"status": "failed", "scan_id": None, "summary": {}, "error": "Store nicht bereit"}
    scan_id: int | None = None
    try:
        target = await resolve_target(store, snapshot, target_id)
        JOBS.set_progress(
            job_id,
            phase="Start",
            percent=5,
            message=f"Scan für {target.name} wird vorbereitet…",
        )
        scan_id = await store.create_scan(
            target_id=target.id, target_name=target.name, status="running"
        )
        JOBS.set_progress(job_id, scan_id=scan_id)

        async def on_progress(phase: str, percent: int, message: str) -> None:
            JOBS.set_progress(job_id, phase=phase, percent=percent, message=message)
            JOBS.append_log(job_id, f"{phase}: {message}")

        result = await scan_target(target, progress=on_progress)
        llm_text = None
        if do_summarize and get_patcher_settings().llm_configured:
            JOBS.set_progress(
                job_id,
                phase="KI",
                percent=90,
                message="KI-Zusammenfassung wird erstellt…",
            )
            JOBS.append_log(job_id, "LLM-Zusammenfassung…")
            try:
                llm_text = await summarize_scan(
                    target_name=target.name,
                    distro=result.get("distro"),
                    pm=result.get("pm"),
                    summary=result.get("summary") or {},
                    packages=result.get("packages") or [],
                )
            except LlmError as exc:
                JOBS.append_log(job_id, f"LLM übersprungen: {exc.message}")
        elif do_summarize:
            JOBS.append_log(job_id, "Kein LLM-Key — nur Heuristik")

        await store.finish_scan(
            scan_id,
            status="success",
            pm=result.get("pm"),
            distro=result.get("distro"),
            summary=result.get("summary"),
            llm_summary=llm_text,
            reboot_required=bool(result.get("reboot_required")),
            packages=result.get("packages") or [],
        )
        summary = result.get("summary") or {}
        JOBS.finish(
            job_id,
            status="success",
            result={
                "scan_id": scan_id,
                "summary": summary,
                "pm": result.get("pm"),
                "distro": result.get("distro"),
                "reboot_required": result.get("reboot_required"),
                "release_upgrade": result.get("release_upgrade"),
                "llm_summary": llm_text,
            },
            message=(
                f"{summary.get('total', 0)} Updates "
                f"({summary.get('security', 0)} Security)"
                + (
                    " — " + (result.get("release_upgrade") or {}).get("headline", "")
                    if (result.get("release_upgrade") or {}).get("headline")
                    else ""
                )
            ),
            phase="Fertig",
        )
        return {
            "status": "success",
            "scan_id": scan_id,
            "summary": summary,
            "release_upgrade": result.get("release_upgrade"),
            "error": None,
        }
    except (ScanError, DockerControlError, RuntimeError) as exc:
        msg = getattr(exc, "message", None) or str(exc)
        if scan_id is not None:
            try:
                await store.finish_scan(scan_id, status="failed", error_message=msg)
            except Exception:
                logger.exception("finish_scan failed")
        JOBS.finish(job_id, status="failed", error=msg)
        return {"status": "failed", "scan_id": scan_id, "summary": {}, "error": msg}
    except Exception as exc:
        logger.exception("scan job failed")
        if scan_id is not None:
            try:
                await store.finish_scan(scan_id, status="failed", error_message=str(exc))
            except Exception:
                logger.exception("finish_scan failed")
        JOBS.finish(job_id, status="failed", error=str(exc))
        return {"status": "failed", "scan_id": scan_id, "summary": {}, "error": str(exc)}


def _join_truncated(items: list[str], *, max_len: int = 100, sep: str = ", ") -> str:
    """Join names until max_len; append ``+N`` for leftovers."""
    if not items:
        return ""
    parts: list[str] = []
    for i, item in enumerate(items):
        leftover = len(items) - i - 1
        suffix = f" +{leftover}" if leftover else ""
        candidate = sep.join(parts + [item])
        if len(candidate) + len(suffix) <= max_len:
            parts.append(item)
            continue
        if not parts:
            room = max_len - 1
            return (item[:room] + "…") if len(item) > room else item
        omitted = len(items) - len(parts)
        return sep.join(parts) + (f" +{omitted}" if omitted else "")
    return sep.join(parts)


def _truncate_push(text: str, max_len: int = 200) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


async def _notify_scan_findings(summary: dict[str, Any]) -> None:
    """Push when daily/manual all-host scan finds updates or errors."""
    hosts_u = int(summary.get("hosts_with_updates") or 0)
    hosts_e = int(summary.get("hosts_with_errors") or 0)
    total = int(summary.get("total_updates") or 0)
    security = int(summary.get("total_security") or 0)
    hosts_rel = int(summary.get("hosts_with_release_upgrade") or 0)
    if hosts_u <= 0 and hosts_e <= 0 and hosts_rel <= 0:
        return
    try:
        from app.core.push import send_push_to_all

        store = None
        try:
            from app.main import app as fastapi_app

            store = getattr(fastapi_app.state, "app_store", None)
        except Exception:
            store = None
        if store is None:
            return

        from app.core.push import push_allowed

        if not await push_allowed(store, "patch_findings"):
            return

        lines: list[str] = []
        if hosts_u:
            lines.append(
                f"{hosts_u} Host(s) · {total} Update(s) ({security} Security)"
            )
            raw_updates = summary.get("update_hosts") or []
            name_bits: list[str] = []
            for h in raw_updates:
                if isinstance(h, dict):
                    name = str(h.get("name") or "").strip()
                    if not name:
                        continue
                    n_upd = h.get("updates")
                    name_bits.append(f"{name} ({n_upd})" if n_upd is not None else name)
                else:
                    name_bits.append(str(h))
            if name_bits:
                lines.append("Updates: " + _join_truncated(name_bits, max_len=110))
        if hosts_rel:
            lines.append(f"{hosts_rel} Host(s) mit Release-Upgrade")
        if hosts_e:
            err_names = [str(x) for x in (summary.get("error_hosts") or []) if x]
            if err_names:
                lines.append("Fehler: " + _join_truncated(err_names, max_len=90))
            else:
                lines.append(f"{hosts_e} Host(s) mit Fehlern")

        body = _truncate_push("\n".join(lines), 200)
        await send_push_to_all(
            store,
            title="HomelabOps — Patch-Check",
            body=body,
            url="/modules/patcher",
            tag="patcher-scan",
        )
    except Exception:
        logger.exception("Push-Benachrichtigung fehlgeschlagen")


async def _scan_one_target_sync(target, *, snapshot, do_summarize: bool = False) -> dict[str, Any]:
    """Run a single-host scan synchronously (for sequential scan-all)."""
    job = JOBS.create(kind="scan", target_id=target.id)
    result = await _run_scan_job(
        job.id,
        target_id=target.id,
        snapshot=snapshot,
        do_summarize=do_summarize,
    )
    return result or {"status": "failed", "summary": {}, "error": "unbekannt"}


async def _run_scan_all(
    *,
    snapshot,
    trigger: str,
    do_summarize: bool = False,
    already_begun: bool = False,
) -> dict[str, Any]:
    store = _store
    if store is None:
        SCAN_ALL.running = False
        return {"ok": False, "error": "Store nicht bereit."}
    all_targets = await list_targets(store, snapshot)
    excluded = await store.list_unmonitored_ids()
    targets = [t for t in all_targets if t.id not in excluded]
    skipped = len(all_targets) - len(targets)
    if skipped:
        logger.info(
            "scan-all: %d Host(s) nicht überwacht — übersprungen", skipped
        )
        SCAN_ALL.message = (
            f"Starte Prüfung ({skipped} nicht überwacht übersprungen)…"
        )

    async def scan_one(target):
        result = await _scan_one_target_sync(
            target, snapshot=snapshot, do_summarize=do_summarize
        )
        SCAN_ALL.message = f"Prüfe Images auf {target.name}…"
        img = await _scan_and_persist_images(target, snapshot)
        result["image_count"] = int((img or {}).get("count") or 0)
        return result

    async def on_complete(summary: dict[str, Any]) -> None:
        try:
            from app.main import app as fastapi_app

            app_store = getattr(fastapi_app.state, "app_store", None)
            if app_store is not None:
                await app_store.set_patcher_last_daily(
                    summary.get("finished_at_iso") or ""
                )
        except Exception:
            logger.exception("patcher last_daily speichern fehlgeschlagen")
        await _notify_scan_findings(summary)
        await _maybe_plan_after_scan(snapshot)

    return await run_scan_all_hosts(
        targets=targets,
        scan_one=scan_one,
        trigger=trigger,
        on_complete=on_complete,
        already_begun=already_begun,
    )


async def _apply_one_target(
    *,
    target,
    store: PatcherStore,
    package_filter: str,
    packages: list[str],
    on_progress,
    on_log,
    reboot_after: bool = False,
    snapshot_first: bool = True,
    proceed_without_snapshot: bool = False,
    via_agent: bool = False,
    snap_holder: dict[str, Any] | None = None,
) -> dict[str, Any]:
    apply_id = await store.create_apply_run(
        target_id=target.id,
        target_name=target.name,
        package_filter=package_filter,
        packages=packages,
        via_agent=via_agent,
    )
    try:
        snap_info = await maybe_pre_snapshot(
            target,
            snapshot_first=snapshot_first,
            proceed_without_snapshot=proceed_without_snapshot,
            on_log=on_log,
            reason="Patch",
            via_agent=via_agent,
        )
        if snap_holder is not None:
            snap_holder.clear()
            snap_holder.update(snap_info)
        result = await apply_updates(
            target,
            package_filter=package_filter,
            packages=packages,
            reboot_after=reboot_after,
            progress=on_progress,
            on_log=on_log,
        )
        await store.finish_apply_run(
            apply_id,
            status="success",
            log_text=result.get("log"),
            reboot_required=bool(result.get("reboot_required")),
            pm=result.get("pm"),
        )
        return {"ok": True, "apply_id": apply_id, "snapshot": snap_info, "via_agent": via_agent, **result}
    except Exception as exc:
        msg = getattr(exc, "message", None) or str(exc)
        try:
            await store.finish_apply_run(
                apply_id, status="failed", error_message=msg
            )
        except Exception:
            logger.exception("finish_apply_run failed")
        raise


async def _run_apply_job(
    job_id: str,
    *,
    target_id: str,
    snapshot,
    package_filter: str,
    packages: list[str],
    reboot_after: bool = False,
    snapshot_first: bool = True,
    proceed_without_snapshot: bool = False,
) -> None:
    store = _store
    if store is None:
        JOBS.finish(job_id, status="failed", error="Store nicht bereit")
        return
    apply_id: int | None = None
    via_agent = _job_via_agent(job_id)
    snap_holder: dict[str, Any] = {}
    try:
        target = await resolve_target(store, snapshot, target_id)
        JOBS.set_progress(
            job_id,
            phase="Start",
            percent=5,
            message=f"Apply für {target.name} wird vorbereitet…",
        )
        JOBS.append_log(
            job_id,
            f"Apply {package_filter} auf {target.name} ({target.ip})"
            + (" — Reboot nach Einspielen." if reboot_after else ""),
        )

        async def on_progress(phase: str, percent: int, message: str) -> None:
            JOBS.set_progress(job_id, phase=phase, percent=percent, message=message)

        async def on_log(line: str) -> None:
            JOBS.append_log(job_id, line)

        result = await _apply_one_target(
            target=target,
            store=store,
            package_filter=package_filter,
            packages=packages,
            on_progress=on_progress,
            on_log=on_log,
            reboot_after=reboot_after,
            snapshot_first=snapshot_first,
            proceed_without_snapshot=proceed_without_snapshot,
            via_agent=via_agent,
            snap_holder=snap_holder,
        )
        apply_id = result.get("apply_id")
        if apply_id is not None:
            try:
                JOBS.set_progress(job_id, apply_id=int(apply_id))
            except (TypeError, ValueError):
                pass
        msg = "Updates eingespielt."
        if result.get("reboot_scheduled"):
            msg = "Updates eingespielt. Reboot wurde geplant."
        elif result.get("reboot_error"):
            msg = f"Updates eingespielt. Reboot fehlgeschlagen: {result.get('reboot_error')}"
        elif result.get("reboot_required"):
            msg += " Reboot empfohlen — bitte manuell bestätigen."
        if via_agent:
            msg = (
                agent_phrase("patches_applied")
                if msg == "Updates eingespielt."
                else by_agent(msg)
            )
        if isinstance(result, dict):
            result = {**result, "via_agent": via_agent}
        JOBS.finish(
            job_id,
            status="success",
            result=result,
            message=msg,
        )
    except (ApplyError, DockerControlError) as exc:
        msg = getattr(exc, "message", None) or str(exc)
        JOBS.finish(
            job_id,
            status="failed",
            error=msg,
            result=_fail_result(snap_holder, via_agent=via_agent),
        )
    except Exception as exc:
        logger.exception("apply job failed")
        JOBS.finish(
            job_id,
            status="failed",
            error=str(exc),
            result=_fail_result(snap_holder, via_agent=via_agent),
        )


async def _run_apply_batch_job(
    job_id: str,
    *,
    target_ids: list[str],
    snapshot,
    package_filter: str,
    reboot_after: bool = False,
    snapshot_first: bool = True,
    proceed_without_snapshot: bool = False,
) -> None:
    store = _store
    if store is None:
        JOBS.finish(job_id, status="failed", error="Store nicht bereit")
        return
    results: list[dict[str, Any]] = []
    total = len(target_ids)
    JOBS.append_log(
        job_id,
        f"Stapel: {total} Host(s), Filter {package_filter}"
        + (
            " — Reboot nach erfolgreichem Einspielen."
            if reboot_after
            else " — kein automatischer Reboot."
        ),
    )
    try:
        for idx, tid in enumerate(target_ids, start=1):
            target = await resolve_target(store, snapshot, tid)
            base = int(((idx - 1) / total) * 100)
            span = max(1, int(100 / total))
            JOBS.set_progress(
                job_id,
                phase=f"{target.name} ({idx}/{total})",
                percent=base,
                message=f"Einspielen {idx}/{total}: {target.name}…",
            )
            JOBS.append_log(job_id, f"=== {target.name} ({idx}/{total}) ===")

            async def on_progress(
                phase: str,
                percent: int,
                message: str,
                *,
                _base=base,
                _span=span,
                _name=target.name,
                _idx=idx,
            ) -> None:
                mapped = _base + int((max(0, min(100, percent)) / 100) * _span)
                JOBS.set_progress(
                    job_id,
                    phase=f"{_name} · {phase} ({_idx}/{total})",
                    percent=min(99, mapped),
                    message=message,
                )

            async def on_log(line: str) -> None:
                JOBS.append_log(job_id, line)

            try:
                result = await _apply_one_target(
                    target=target,
                    store=store,
                    package_filter=package_filter,
                    packages=[],
                    on_progress=on_progress,
                    on_log=on_log,
                    reboot_after=reboot_after,
                    snapshot_first=snapshot_first,
                    proceed_without_snapshot=proceed_without_snapshot,
                )
                entry = {
                    "ok": True,
                    "target_id": target.id,
                    "target_name": target.name,
                    "apply_id": result.get("apply_id"),
                    "reboot_required": bool(result.get("reboot_required")),
                    "reboot_scheduled": bool(result.get("reboot_scheduled")),
                    "reboot_error": result.get("reboot_error"),
                    "error": None,
                }
                results.append(entry)
                if entry["reboot_scheduled"]:
                    JOBS.append_log(job_id, f"{target.name}: Reboot geplant.")
                elif entry["reboot_error"]:
                    JOBS.append_log(
                        job_id,
                        f"{target.name}: Reboot fehlgeschlagen — {entry['reboot_error']}",
                    )
                elif entry["reboot_required"]:
                    JOBS.append_log(
                        job_id,
                        f"{target.name}: Reboot empfohlen (nicht automatisch).",
                    )
            except (ApplyError, DockerControlError) as exc:
                msg = getattr(exc, "message", None) or str(exc)
                JOBS.append_log(job_id, f"{target.name}: Fehler — {msg}")
                results.append(
                    {
                        "ok": False,
                        "target_id": target.id,
                        "target_name": target.name,
                        "apply_id": None,
                        "reboot_required": False,
                        "reboot_scheduled": False,
                        "reboot_error": None,
                        "error": msg,
                    }
                )
            except Exception as exc:
                logger.exception("batch apply failed for %s", tid)
                JOBS.append_log(job_id, f"{target.name}: Fehler — {exc}")
                results.append(
                    {
                        "ok": False,
                        "target_id": getattr(target, "id", tid),
                        "target_name": getattr(target, "name", tid),
                        "apply_id": None,
                        "reboot_required": False,
                        "reboot_scheduled": False,
                        "reboot_error": None,
                        "error": str(exc),
                    }
                )

        ok_n = sum(1 for r in results if r.get("ok"))
        fail_n = len(results) - ok_n
        rebooted_names = [
            str(r.get("target_name"))
            for r in results
            if r.get("ok") and r.get("reboot_scheduled")
        ]
        reboot_fail_names = [
            str(r.get("target_name"))
            for r in results
            if r.get("ok") and r.get("reboot_error")
        ]
        reboot_names = [
            str(r.get("target_name"))
            for r in results
            if r.get("ok") and r.get("reboot_required") and not r.get("reboot_scheduled")
        ]
        msg = f"{ok_n}/{len(results)} Host(s) eingespielt."
        if fail_n:
            msg += f" {fail_n} Fehler."
        if rebooted_names:
            msg += " Reboot geplant: " + ", ".join(rebooted_names)
        if reboot_fail_names:
            msg += " Reboot fehlgeschlagen: " + ", ".join(reboot_fail_names)
        if reboot_names:
            msg += " Reboot empfohlen: " + ", ".join(reboot_names)
        JOBS.finish(
            job_id,
            status="success" if fail_n == 0 else "failed",
            result={
                "hosts": results,
                "reboot_hosts": reboot_names,
                "rebooted_hosts": rebooted_names,
            },
            message=msg,
            phase="Fertig" if fail_n == 0 else "Teilweise fehlgeschlagen",
        )
    except Exception as exc:
        logger.exception("apply-batch job failed")
        JOBS.finish(job_id, status="failed", error=str(exc))


async def _run_release_upgrade_job(
    job_id: str,
    *,
    target_id: str,
    snapshot,
    reboot_after: bool = False,
    snapshot_first: bool = True,
    proceed_without_snapshot: bool = False,
) -> None:
    store = _store
    if store is None:
        JOBS.finish(job_id, status="failed", error="Store nicht bereit")
        return
    try:
        target = await resolve_target(store, snapshot, target_id)
        JOBS.set_progress(
            job_id,
            phase="Start",
            percent=3,
            message=f"Release-Upgrade für {target.name} wird vorbereitet…",
        )
        JOBS.append_log(
            job_id,
            f"Release-Upgrade auf {target.name} ({target.ip})"
            + (" — Reboot am Ende." if reboot_after else " — kein automatischer Schluss-Reboot."),
        )

        async def on_progress(phase: str, percent: int, message: str) -> None:
            JOBS.set_progress(job_id, phase=phase, percent=percent, message=message)

        async def on_log(line: str) -> None:
            JOBS.append_log(job_id, line)

        apply_id = await store.create_apply_run(
            target_id=target.id,
            target_name=target.name,
            package_filter="release-upgrade",
            packages=[],
        )
        try:
            await maybe_pre_snapshot(
                target,
                snapshot_first=snapshot_first,
                proceed_without_snapshot=proceed_without_snapshot,
                on_log=on_log,
                reason="Release-Upgrade",
            )
            result = await perform_release_upgrade(
                target,
                reboot_after=reboot_after,
                progress=on_progress,
                on_log=on_log,
            )
            await store.finish_apply_run(
                apply_id,
                status="success",
                log_text=result.get("log"),
                reboot_required=bool(result.get("reboot_required")),
                pm=result.get("pm"),
            )
        except Exception as exc:
            msg = getattr(exc, "message", None) or str(exc)
            try:
                await store.finish_apply_run(
                    apply_id, status="failed", error_message=msg
                )
            except Exception:
                logger.exception("finish_apply_run failed")
            raise

        JOBS.set_progress(job_id, apply_id=int(apply_id))
        JOBS.set_progress(
            job_id, phase="Scan", percent=97, message="OS-Version nach dem Upgrade prüfen…"
        )
        try:
            scan_id = await store.create_scan(
                target_id=target.id, target_name=target.name, status="running"
            )
            scanned = await scan_target(target)
            await store.finish_scan(
                scan_id,
                status="success",
                pm=scanned.get("pm"),
                distro=scanned.get("distro"),
                summary=scanned.get("summary"),
                reboot_required=bool(scanned.get("reboot_required")),
                packages=scanned.get("packages") or [],
            )
            JOBS.append_log(
                job_id,
                f"Nach-Scan: {scanned.get('distro') or 'ok'}",
            )
        except Exception:
            logger.exception("post-release scan failed")
            JOBS.append_log(job_id, "Nach-Scan fehlgeschlagen — bitte manuell scannen.")

        msg = f"Release-Upgrade fertig: {result.get('distro') or target.name}."
        if result.get("reboot_scheduled"):
            msg += " Reboot wurde geplant."
        elif result.get("reboot_error"):
            msg += f" Reboot fehlgeschlagen: {result.get('reboot_error')}"
        elif result.get("reboot_required"):
            msg += " Reboot empfohlen — bitte manuell bestätigen."
        JOBS.finish(
            job_id,
            status="success",
            result={**result, "apply_id": apply_id},
            message=msg,
        )
    except (ApplyError, DockerControlError) as exc:
        msg = getattr(exc, "message", None) or str(exc)
        JOBS.finish(job_id, status="failed", error=msg)
    except Exception as exc:
        logger.exception("release-upgrade job failed")
        JOBS.finish(job_id, status="failed", error=str(exc))


# --- API ---


@router.get("/status")
async def module_status(request: Request) -> dict[str, Any]:
    ps = get_patcher_settings()
    s = get_settings()
    store = _get_store()
    snap = _snapshot(request)
    targets = await list_targets(store, snap)
    excluded = await store.list_unmonitored_ids()
    unmon = sum(1 for t in targets if t.id in excluded)
    policy = None
    wave = None
    if _engine is not None:
        try:
            p = await _engine.policy()
            policy = {
                "enabled": p.enabled,
                "auto_security": p.auto_security,
                "max_parallel": p.max_parallel,
            }
            wave = await _engine.current_wave()
        except Exception:
            logger.exception("agent status")
    return {
        "module": "patcher",
        "version": "0.1.0",
        "time": format_de(now_berlin()),
        "ssh_key_present": ssh_key_present(s),
        "llm_configured": ps.llm_configured,
        "llm_model": ps.patcher_llm_model if ps.llm_configured else None,
        "target_count": len(targets),
        "monitored_count": len(targets) - unmon,
        "unmonitored_count": unmon,
        "crontab": cron_mod.crontab_available(),
        "agent": policy
        or {
            "enabled": ps.patcher_agent_enabled,
            "auto_security": ps.patcher_agent_auto_security,
            "max_parallel": ps.patcher_agent_max_parallel,
        },
        "wave": wave,
    }


@router.get("/targets")
async def api_targets(request: Request) -> dict[str, Any]:
    store = _get_store()
    items = await _enrich_targets(store, _snapshot(request))
    unmon = sum(1 for t in items if not t.get("monitored", True))
    return {
        "targets": items,
        "count": len(items),
        "monitored_count": len(items) - unmon,
        "unmonitored_count": unmon,
    }


@router.post("/targets/monitor")
async def api_set_monitor(payload: MonitorPayload, request: Request) -> dict[str, Any]:
    store = _get_store()
    snap = _snapshot(request)
    known = {t.id for t in await list_targets(store, snap)}
    tid = payload.target_id.strip()
    if tid not in known:
        raise HTTPException(status_code=404, detail="Ziel nicht gefunden.")
    await store.set_monitored(tid, payload.monitored)
    return {"ok": True, "target_id": tid, "monitored": payload.monitored}


@router.get("/scans/{scan_id}")
async def api_scan_detail(scan_id: int) -> dict[str, Any]:
    store = _get_store()
    scan = await store.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan nicht gefunden.")
    packages = await store.list_packages(scan_id)
    return {"scan": scan, "packages": packages}


@router.get("/history")
async def api_history(
    limit: int = Query(default=40, ge=1, le=200),
) -> dict[str, Any]:
    store = _get_store()
    scans = await store.list_scans(limit=limit)
    applies = await store.list_apply_runs(limit=limit)
    for run in applies:
        if run.get("status") in ("failed", "running") and not run.get("explanation"):
            run["explanation"] = explain_apply_run(run)
        elif run.get("status") == "failed":
            run["explanation"] = run.get("explanation") or explain_apply_run(run)
    return {"scans": scans, "apply_runs": applies}


@router.post("/scan")
async def api_scan(
    payload: ScanPayload,
    request: Request,
    background: BackgroundTasks,
) -> dict[str, Any]:
    store = _get_store()
    snap = _snapshot(request)
    try:
        target = await resolve_target(store, snap, payload.target_id)
    except DockerControlError as exc:
        raise _http_docker(exc) from exc

    job = JOBS.create(kind="scan", target_id=target.id)
    if payload.wait:
        await _run_scan_job(
            job.id,
            target_id=target.id,
            snapshot=snap,
            do_summarize=payload.summarize,
        )
        done = JOBS.get(job.id)
        return done.to_dict() if done else {"job_id": job.id, "status": "unknown"}

    background.add_task(
        _run_scan_job,
        job.id,
        target_id=target.id,
        snapshot=snap,
        do_summarize=payload.summarize,
    )
    return job.to_dict()


@router.get("/scan-all/status")
async def api_scan_all_status() -> dict[str, Any]:
    ps = get_patcher_settings()
    return {
        **SCAN_ALL.to_dict(),
        "daily_enabled": ps.patcher_daily_enabled,
        "daily_hour": ps.patcher_daily_hour,
        "daily_cron": ps.patcher_cron or None,
    }


@router.post("/scan-all")
async def api_scan_all(
    request: Request,
    background: BackgroundTasks,
    wait: bool = False,
) -> dict[str, Any]:
    """Sequentially scan all patch targets (manual „Alle Hosts prüfen“)."""
    begun = await begin_scan_all(trigger="manual")
    if begun is None:
        raise HTTPException(status_code=409, detail="Scan läuft bereits.")
    snap = _snapshot(request)
    if wait:
        return await _run_scan_all(
            snapshot=snap, trigger="manual", do_summarize=False, already_begun=True
        )
    background.add_task(
        _run_scan_all,
        snapshot=snap,
        trigger="manual",
        do_summarize=False,
        already_begun=True,
    )
    return {"ok": True, "started": True, **SCAN_ALL.to_dict()}


@router.post("/apply")
async def api_apply(
    payload: ApplyPayload,
    request: Request,
    background: BackgroundTasks,
) -> dict[str, Any]:
    if not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail="Apply erfordert confirm=true.",
        )
    store = _get_store()
    snap = _snapshot(request)
    try:
        target = await resolve_target(store, snap, payload.target_id)
    except DockerControlError as exc:
        raise _http_docker(exc) from exc

    job = JOBS.create(kind="apply", target_id=target.id)
    if payload.wait:
        await _run_apply_job(
            job.id,
            target_id=target.id,
            snapshot=snap,
            package_filter=payload.package_filter,
            packages=payload.packages,
            reboot_after=payload.reboot_after,
            snapshot_first=payload.snapshot_first,
            proceed_without_snapshot=payload.proceed_without_snapshot,
        )
        done = JOBS.get(job.id)
        return done.to_dict() if done else {"job_id": job.id, "status": "unknown"}

    background.add_task(
        _run_apply_job,
        job.id,
        target_id=target.id,
        snapshot=snap,
        package_filter=payload.package_filter,
        packages=payload.packages,
        reboot_after=payload.reboot_after,
        snapshot_first=payload.snapshot_first,
        proceed_without_snapshot=payload.proceed_without_snapshot,
    )
    return job.to_dict()


@router.post("/apply-batch")
async def api_apply_batch(
    payload: ApplyBatchPayload,
    request: Request,
    background: BackgroundTasks,
) -> dict[str, Any]:
    if not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail="Apply erfordert confirm=true.",
        )
    ids: list[str] = []
    seen: set[str] = set()
    for raw in payload.target_ids:
        tid = (raw or "").strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        ids.append(tid)
    if not ids:
        raise HTTPException(status_code=400, detail="Keine Hosts ausgewählt.")
    if len(ids) > 50:
        raise HTTPException(status_code=400, detail="Maximal 50 Hosts pro Stapel.")

    store = _get_store()
    snap = _snapshot(request)
    for tid in ids:
        try:
            await resolve_target(store, snap, tid)
        except DockerControlError as exc:
            raise _http_docker(exc) from exc

    job = JOBS.create(kind="apply-batch", target_id="")
    if payload.wait:
        await _run_apply_batch_job(
            job.id,
            target_ids=ids,
            snapshot=snap,
            package_filter=payload.package_filter,
            reboot_after=payload.reboot_after,
            snapshot_first=payload.snapshot_first,
            proceed_without_snapshot=payload.proceed_without_snapshot,
        )
        done = JOBS.get(job.id)
        return done.to_dict() if done else {"job_id": job.id, "status": "unknown"}

    background.add_task(
        _run_apply_batch_job,
        job.id,
        target_ids=ids,
        snapshot=snap,
        package_filter=payload.package_filter,
        reboot_after=payload.reboot_after,
        snapshot_first=payload.snapshot_first,
        proceed_without_snapshot=payload.proceed_without_snapshot,
    )
    return job.to_dict()


@router.post("/release-upgrade")
async def api_release_upgrade(
    payload: ReleaseUpgradePayload,
    request: Request,
    background: BackgroundTasks,
) -> dict[str, Any]:
    if not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail="Release-Upgrade erfordert confirm=true.",
        )
    store = _get_store()
    snap = _snapshot(request)
    try:
        target = await resolve_target(store, snap, payload.target_id)
    except DockerControlError as exc:
        raise _http_docker(exc) from exc

    job = JOBS.create(kind="release-upgrade", target_id=target.id)
    if payload.wait:
        await _run_release_upgrade_job(
            job.id,
            target_id=target.id,
            snapshot=snap,
            reboot_after=payload.reboot_after,
            snapshot_first=payload.snapshot_first,
            proceed_without_snapshot=payload.proceed_without_snapshot,
        )
        done = JOBS.get(job.id)
        return done.to_dict() if done else {"job_id": job.id, "status": "unknown"}

    background.add_task(
        _run_release_upgrade_job,
        job.id,
        target_id=target.id,
        snapshot=snap,
        reboot_after=payload.reboot_after,
        snapshot_first=payload.snapshot_first,
        proceed_without_snapshot=payload.proceed_without_snapshot,
    )
    return job.to_dict()


@router.post("/images/scan")
async def api_image_scan(
    payload: ImageScanPayload,
    request: Request,
    background: BackgroundTasks,
) -> dict[str, Any]:
    store = _get_store()
    snap = _snapshot(request)
    try:
        target = await resolve_target(store, snap, payload.target_id)
    except DockerControlError as exc:
        raise _http_docker(exc) from exc

    job = JOBS.create(kind="image-scan", target_id=target.id)
    if payload.wait:
        await _run_image_scan_job(job.id, target_id=target.id, snapshot=snap)
        done = JOBS.get(job.id)
        return done.to_dict() if done else {"job_id": job.id, "status": "unknown"}

    background.add_task(
        _run_image_scan_job, job.id, target_id=target.id, snapshot=snap
    )
    return job.to_dict()


@router.post("/images/apply")
async def api_image_apply(
    payload: ImageApplyPayload,
    request: Request,
    background: BackgroundTasks,
) -> dict[str, Any]:
    if not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail="Image-Update erfordert confirm=true.",
        )
    store = _get_store()
    snap = _snapshot(request)
    try:
        target = await resolve_target(store, snap, payload.target_id)
    except DockerControlError as exc:
        raise _http_docker(exc) from exc

    names = [n.strip() for n in payload.names if (n or "").strip()]
    if not names:
        latest = await store.latest_image_scan_for_target(target.id)
        raw = ((latest or {}).get("summary") or {}).get("updates") or []
        names = [
            str(u.get("name"))
            for u in raw
            if isinstance(u, dict) and u.get("name")
        ]
    if not names:
        raise HTTPException(
            status_code=400,
            detail="Keine Image-Updates zum Einspielen (bitte zuerst Images prüfen).",
        )

    job = JOBS.create(kind="image-apply", target_id=target.id)
    if payload.wait:
        await _run_image_apply_job(
            job.id,
            target_id=target.id,
            snapshot=snap,
            names=names,
            restart=payload.restart,
            prune=payload.prune,
            snapshot_first=payload.snapshot_first,
            proceed_without_snapshot=payload.proceed_without_snapshot,
        )
        done = JOBS.get(job.id)
        return done.to_dict() if done else {"job_id": job.id, "status": "unknown"}

    background.add_task(
        _run_image_apply_job,
        job.id,
        target_id=target.id,
        snapshot=snap,
        names=names,
        restart=payload.restart,
        prune=payload.prune,
        snapshot_first=payload.snapshot_first,
        proceed_without_snapshot=payload.proceed_without_snapshot,
    )
    return job.to_dict()


@router.post("/reboot")
async def api_reboot(payload: RebootPayload, request: Request) -> dict[str, Any]:
    store = _get_store()
    try:
        target = await resolve_target(store, _snapshot(request), payload.target_id)
        result = await reboot_host(target, confirm=payload.confirm)
    except (ApplyError, DockerControlError) as exc:
        msg = getattr(exc, "message", None) or str(exc)
        code = getattr(exc, "status_code", 400)
        raise HTTPException(status_code=code, detail=msg) from exc
    return result


@router.post("/summarize")
async def api_summarize(payload: SummarizePayload) -> dict[str, Any]:
    store = _get_store()
    scan = await store.get_scan(payload.scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan nicht gefunden.")
    packages = await store.list_packages(payload.scan_id)
    try:
        text = await summarize_scan(
            target_name=scan.get("target_name") or scan.get("target_id"),
            distro=scan.get("distro"),
            pm=scan.get("pm"),
            summary=scan.get("summary") or {},
            packages=packages,
        )
    except LlmError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    await store.set_scan_llm_summary(payload.scan_id, text)
    return {"ok": True, "scan_id": payload.scan_id, "llm_summary": text}


@router.get("/jobs")
async def api_jobs(active: bool = True) -> dict[str, Any]:
    jobs = JOBS.list_active() if active else []
    return {"ok": True, "jobs": [await _job_payload(j) for j in jobs]}


@router.get("/jobs/{job_id}")
async def api_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden.")
    return await _job_payload(job)


@router.get("/agent")
async def api_agent_status() -> dict[str, Any]:
    engine = _get_engine()
    policy = await engine.policy()
    wave = await engine.current_wave()
    return {
        "ok": True,
        "enabled": policy.enabled,
        "auto_security": policy.auto_security,
        "max_parallel": policy.max_parallel,
        "wave": wave,
    }


@router.post("/agent/settings")
async def api_agent_settings(payload: AgentSettingsPayload) -> dict[str, Any]:
    engine = _get_engine()
    try:
        policy = await engine.save_policy(
            enabled=payload.enabled,
            auto_security=payload.auto_security,
            max_parallel=payload.max_parallel,
        )
    except Exception as exc:
        raise _agent_http(exc) from exc
    return {
        "ok": True,
        "enabled": policy.enabled,
        "auto_security": policy.auto_security,
        "max_parallel": policy.max_parallel,
    }


@router.post("/agent/plan")
async def api_agent_plan(request: Request) -> dict[str, Any]:
    engine = _get_engine()
    policy = await engine.policy()
    if not policy.enabled:
        raise HTTPException(
            status_code=400,
            detail="Wellen-Agent ist aus. Unter Zeitplan oder per PATCHER_AGENT_ENABLED einschalten.",
        )
    snap = _snapshot(request)
    try:
        hosts = await hosts_from_store(
            engine.store, snap, tags_for=_inventory_tags
        )
        wave = await engine.plan(hosts)
    except RuntimeError as exc:
        raise _agent_http(exc) from exc
    return {"ok": True, "wave": wave}


@router.post("/agent/start")
async def api_agent_start() -> dict[str, Any]:
    engine = _get_engine()
    try:
        wave = await engine.start()
    except RuntimeError as exc:
        raise _agent_http(exc) from exc
    return {"ok": True, "wave": wave}


@router.post("/agent/stop")
async def api_agent_stop() -> dict[str, Any]:
    engine = _get_engine()
    try:
        wave = await engine.stop()
    except RuntimeError as exc:
        raise _agent_http(exc) from exc
    return {"ok": True, "wave": wave}


@router.post("/agent/confirm")
async def api_agent_confirm(payload: AgentConfirmPayload) -> dict[str, Any]:
    engine = _get_engine()
    try:
        wave = await engine.confirm(
            item_ids=payload.item_ids,
            all_waiting=payload.all_waiting,
        )
    except RuntimeError as exc:
        raise _agent_http(exc) from exc
    return {"ok": True, "wave": wave}


# --- hosts CRUD ---


@router.get("/hosts")
async def api_list_hosts() -> dict[str, Any]:
    store = _get_store()
    return {"hosts": await store.list_hosts()}


@router.post("/hosts")
async def api_upsert_host(payload: HostPayload) -> dict[str, Any]:
    store = _get_store()
    hid = await store.upsert_host(
        host_id=payload.id,
        name=payload.name.strip(),
        host=payload.host.strip(),
        port=payload.port,
        ssh_user=payload.ssh_user,
        enabled=payload.enabled,
        note=payload.note,
    )
    row = await store.get_host(hid)
    return {"ok": True, "host": row}


@router.delete("/hosts/{host_id}")
async def api_delete_host(host_id: int) -> dict[str, Any]:
    store = _get_store()
    await store.delete_host(host_id)
    return {"ok": True}


@router.post("/hosts/check")
async def api_check_host(payload: HostCheckPayload) -> dict[str, Any]:
    ps = get_patcher_settings()
    try:
        result = await ssh_probe(
            payload.host.strip(),
            username=payload.ssh_user,
            port=payload.port,
            connect_timeout=ps.patcher_connect_timeout,
        )
    except DockerControlError as exc:
        raise _http_docker(exc) from exc
    return result


# --- schedules ---


@router.get("/schedules")
async def api_list_schedules() -> dict[str, Any]:
    store = _get_store()
    return {
        "schedules": await store.list_schedules(),
        "crontab_available": cron_mod.crontab_available(),
    }


@router.post("/schedules")
async def api_upsert_schedule(payload: SchedulePayload) -> dict[str, Any]:
    store = _get_store()
    try:
        if payload.preset == "custom":
            if not payload.cron_expr:
                raise cron_mod.CronError("cron_expr erforderlich bei preset=custom")
            expr = cron_mod.validate_cron_expr(payload.cron_expr)
        else:
            expr = cron_mod.preset_to_cron(
                payload.preset, payload.time, payload.weekday
            )
    except cron_mod.CronError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    sid = await store.upsert_schedule(
        schedule_id=payload.id,
        target_id=payload.target_id,
        cron_expr=expr,
        preset=payload.preset,
        enabled=payload.enabled,
        note=payload.note,
    )
    schedules = await store.list_schedules()
    sync = cron_mod.sync_crontab(schedules)
    return {"ok": True, "id": sid, "cron_expr": expr, "crontab": sync}


@router.delete("/schedules/{schedule_id}")
async def api_delete_schedule(schedule_id: int) -> dict[str, Any]:
    store = _get_store()
    await store.delete_schedule(schedule_id)
    schedules = await store.list_schedules()
    sync = cron_mod.sync_crontab(schedules)
    return {"ok": True, "crontab": sync}


class PatcherModule:
    name = "patcher"
    version = "0.1.0"
    description = (
        "AI-Driven Patch-Management: Updates auf Proxmox-Guests und manuellen "
        "Linux-Hosts scannen, melden und mit Bestätigung einspielen (apt/dnf/apk). "
        "Ubuntu-Release-Upgrade nur nach Confirm (nie automatisch)."
    )
    enabled = True
    meta = {
        "phase": 2,
        "role": "patch",
        "ui_path": "/modules/patcher",
    }

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app: FastAPI) -> None:
        global _store, _daily_task, _engine
        ps = get_patcher_settings()
        _store = PatcherStore(ps.db_path)
        await _store.connect()
        app.state.patcher_store = _store

        def _snap() -> Any:
            store: TopologyStore | None = getattr(app.state, "topology_store", None)
            return store.snapshot if store is not None else None

        _engine = WaveEngine(
            _store,
            apply_job=_run_apply_job,
            image_apply_job=_run_image_apply_job,
            get_snapshot=_snap,
            get_inventory_tags=_inventory_tags,
        )
        app.state.patcher_wave_engine = _engine
        try:
            await _engine.resume_if_needed()
        except Exception:
            logger.exception("Welle nach Start nicht fortgesetzt")

        engine = getattr(app.state, "discovery_engine", None)
        if engine is not None and hasattr(engine, "set_manual_hosts_provider"):
            store_ref = _store

            async def _manual_hosts() -> list[dict[str, Any]]:
                return await store_ref.list_hosts(enabled_only=True)

            engine.set_manual_hosts_provider(_manual_hosts)

        templates = _make_templates()
        settings = get_settings()

        def _page_ctx(active_view: str) -> dict[str, Any]:
            return {
                "app_name": settings.app_name,
                "app_version": settings.app_version,
                "now": format_de(now_berlin()),
                "module_version": self.version,
                "active_view": active_view,
                "llm_configured": ps.llm_configured,
            }

        @app.get("/modules/patcher", response_class=HTMLResponse)
        async def patcher_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "page.html",
                _page_ctx("overview"),
            )

        @app.get("/modules/patcher/hosts", response_class=HTMLResponse)
        async def patcher_hosts_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "hosts.html",
                _page_ctx("hosts"),
            )

        @app.get("/modules/patcher/history", response_class=HTMLResponse)
        async def patcher_history_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "history.html",
                _page_ctx("history"),
            )

        @app.get("/modules/patcher/schedule", response_class=HTMLResponse)
        async def patcher_schedule_page(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "schedule.html",
                _page_ctx("schedule"),
            )

        async def _daily_runner() -> None:
            async def get_targets_and_scan() -> None:
                store: TopologyStore = app.state.topology_store
                snap = store.snapshot
                logger.info("Patcher Daily-Scan startet…")
                await _run_scan_all(
                    snapshot=snap, trigger="daily", do_summarize=False
                )

            await daily_scan_loop(
                enabled=ps.patcher_daily_enabled,
                cron_expr=ps.patcher_cron,
                hour=ps.patcher_daily_hour,
                get_targets_and_scan=get_targets_and_scan,
            )

        if ps.patcher_daily_enabled:
            _daily_task = asyncio.create_task(
                _daily_runner(), name="patcher-daily-scan"
            )
            app.state.patcher_daily_task = _daily_task

        logger.info(
            "patcher ready — DB %s · daily=%s hour=%s cron=%r agent=%s (OS + Images)",
            ps.db_path,
            ps.patcher_daily_enabled,
            ps.patcher_daily_hour,
            ps.patcher_cron or "",
            ps.patcher_agent_enabled,
        )

    async def on_shutdown(self, app: FastAPI) -> None:
        global _store, _daily_task, _engine
        wave_task = getattr(_engine, "_task", None) if _engine else None
        if wave_task is not None and not wave_task.done():
            wave_task.cancel()
            try:
                await wave_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        _engine = None
        task = getattr(app.state, "patcher_daily_task", None) or _daily_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            _daily_task = None
        if _store:
            await _store.close()
            _store = None

    async def on_topology_refresh(self, topology: dict[str, Any]) -> None:
        n = len(topology.get("guests") or [])
        logger.debug("patcher topology refresh: %s guests", n)


MODULE = PatcherModule()
