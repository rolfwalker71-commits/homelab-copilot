"""In-process health poll + disk alerts (Europe/Berlin)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.locale import now_berlin
from app.core.push import push_allowed, send_push_to_all

from health.checker import check_url
from health.config import get_health_settings
from health.store import HealthStore

logger = logging.getLogger(__name__)


def _today() -> str:
    return now_berlin().strftime("%Y-%m-%d")


async def _app_store():
    try:
        from app.main import app as fastapi_app

        return getattr(fastapi_app.state, "app_store", None)
    except Exception:
        return None


async def run_one_check(store: HealthStore, row: dict[str, Any]) -> dict[str, Any]:
    hs = get_health_settings()
    prev = str(row.get("last_status") or "unknown")
    result = await check_url(row["url"], timeout=hs.health_http_timeout)
    updated = await store.record_result(
        int(row["id"]),
        status=result["status"],
        http_code=result.get("http_code"),
        error=result.get("error"),
        cert_days_left=result.get("cert_days_left"),
        cert_not_after=result.get("cert_not_after"),
    )
    app_store = await _app_store()
    if app_store is None:
        return updated

    label = updated.get("label") or updated.get("url")
    if (
        result["status"] == "down"
        and prev != "down"
        and await push_allowed(app_store, "health_down")
    ):
        await send_push_to_all(
            app_store,
            title="HomelabOps — Host down",
            body=f"{label} nicht erreichbar"
            + (f" ({result.get('error')})" if result.get("error") else ""),
            url="/modules/health",
            tag=f"health-down-{updated['id']}",
        )
        await store.mark_down_notified(int(updated["id"]))

    days = result.get("cert_days_left")
    if (
        days is not None
        and days <= hs.health_cert_warn_days
        and str(updated.get("last_cert_push_date") or "") != _today()
    ):
        # Cert warning is part of health_down family; reuse same toggle.
        if await push_allowed(app_store, "health_down"):
            await send_push_to_all(
                app_store,
                title="HomelabOps — Zertifikat",
                body=f"{label}: TLS läuft in {days} Tag(en) ab.",
                url="/modules/health",
                tag=f"health-cert-{updated['id']}",
            )
            await store.mark_cert_pushed(int(updated["id"]), _today())
    return updated


async def poll_all_checks(store: HealthStore) -> dict[str, Any]:
    rows = await store.list_checks(enabled_only=True)
    down = 0
    for row in rows:
        try:
            updated = await run_one_check(store, row)
            if updated.get("last_status") == "down":
                down += 1
        except Exception:
            logger.exception("Health-Check %s fehlgeschlagen", row.get("url"))
    return {"checked": len(rows), "down": down}


def _disk_pct(entity: dict[str, Any]) -> float | None:
    meta = entity.get("meta") or {}
    raw = meta.get("disk_pct")
    try:
        if raw is not None:
            return float(raw)
    except (TypeError, ValueError):
        return None
    return None


async def poll_disk_alerts(store: HealthStore, topology: dict[str, Any] | None) -> None:
    if not topology:
        return
    hs = get_health_settings()
    app_store = await _app_store()
    if app_store is None or not await push_allowed(app_store, "disk_high"):
        return
    today = _today()
    entities: list[dict[str, Any]] = []
    entities.extend(topology.get("nodes") or [])
    entities.extend(topology.get("guests") or [])
    entities.extend(topology.get("hosts") or [])

    for ent in entities:
        if not isinstance(ent, dict):
            continue
        eid = str(ent.get("id") or "")
        name = str(ent.get("name") or eid)
        pct = _disk_pct(ent)
        if pct is None and str(eid).startswith("manual:"):
            try:
                from app.main import app as fastapi_app

                engine = getattr(fastapi_app.state, "discovery_engine", None)
                snap = getattr(
                    getattr(fastapi_app.state, "topology_store", None),
                    "snapshot",
                    None,
                )
                from app.config import get_settings
                from app.core.ssh_endpoint import resolve_ssh_endpoint

                ep = resolve_ssh_endpoint(snap, eid, get_settings())
                if engine is not None and ep is not None:
                    facts = await engine.fetch_host_facts(
                        ep.ip, port=ep.port, username=ep.username
                    )
                    disk = facts.get("disk") or {}
                    if disk.get("pct") is not None:
                        pct = float(disk["pct"])
            except Exception:
                logger.debug("SSH-Disk für %s nicht lesbar", eid)
        if pct is None or pct < hs.health_disk_warn_pct:
            continue
        prev = await store.get_disk_alert(eid)
        if prev and str(prev.get("last_push_date") or "") == today:
            continue
        await send_push_to_all(
            app_store,
            title="HomelabOps — Disk voll",
            body=f"{name}: Disk {pct:.0f} % (Schwelle {hs.health_disk_warn_pct:.0f} %).",
            url="/",
            tag=f"disk-{eid}",
        )
        await store.set_disk_alert(eid, pct=pct, push_date=today)


async def health_loop(store: HealthStore) -> None:
    hs = get_health_settings()
    interval = max(60, int(hs.health_poll_interval_seconds))
    logger.info("Health-Poll aktiv — alle %ss", interval)
    await asyncio.sleep(8)
    while True:
        try:
            await poll_all_checks(store)
            topology = None
            try:
                from app.main import app as fastapi_app

                snap = getattr(
                    getattr(fastapi_app.state, "topology_store", None),
                    "snapshot",
                    None,
                )
                if snap is not None:
                    topology = snap.model_dump()
            except Exception:
                topology = None
            await poll_disk_alerts(store, topology)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Health-Poll fehlgeschlagen")
        await asyncio.sleep(interval)
