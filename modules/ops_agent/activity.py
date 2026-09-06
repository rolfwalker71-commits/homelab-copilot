"""Durable Tätigkeitslog + Abend-Kurzlage (kein LLM)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.locale import BERLIN, now_berlin
from ops_agent.actor import VIA_AGENT

ACTION_PLANNED = "planned"
ACTION_STARTED = "started"
ACTION_SHIFTED = "shifted"
ACTION_SKIPPED = "skipped"
ACTION_BACKUP_CHAIN = "backup_chain"
ACTION_APPLY = "apply"
ACTION_ROLLBACK = "rollback"
ACTION_REBOOT = "reboot"
ACTION_PRUNE = "prune"
ACTION_WARN = "warn"
ACTION_BRIEF = "brief"

ACTION_LABELS_DE = {
    ACTION_PLANNED: "Geplant",
    ACTION_STARTED: "Gestartet",
    ACTION_SHIFTED: "Verschoben",
    ACTION_SKIPPED: "Übersprungen",
    ACTION_BACKUP_CHAIN: "Backup-Kette",
    ACTION_APPLY: "Apply",
    ACTION_ROLLBACK: "Rollback",
    ACTION_REBOOT: "Reboot",
    ACTION_PRUNE: "Image-Prune",
    ACTION_WARN: "Warnung",
    ACTION_BRIEF: "Abend-Kurzlage",
}

RESULT_OK = "ok"
RESULT_FAIL = "fail"
RESULT_WAIT = "wait"
RESULT_SKIP = "skip"
RESULT_INFO = "info"


def action_label_de(action: str) -> str:
    return ACTION_LABELS_DE.get(str(action or ""), str(action or "Eintrag"))


def serialize_activity(row: dict[str, Any]) -> dict[str, Any]:
    action = str(row.get("action") or "")
    return {
        **row,
        "action_label": action_label_de(action),
        "actor_label": VIA_AGENT if row.get("via_agent") else "",
    }


def _names(rows: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        name = str(row.get("target_name") or row.get("target_id") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _join_de(items: list[str], *, limit: int = 6) -> str:
    if not items:
        return ""
    shown = items[:limit]
    extra = len(items) - len(shown)
    text = ", ".join(shown)
    if extra > 0:
        text = f"{text} (+{extra})"
    return text


def _day_iso(now: datetime) -> str:
    local = now.astimezone(BERLIN) if now.tzinfo else now.replace(tzinfo=BERLIN)
    return local.date().isoformat()


def filter_today(entries: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    day = _day_iso(now or now_berlin())
    out: list[dict[str, Any]] = []
    for row in entries:
        iso = str(row.get("created_at_iso") or "")
        if not iso:
            continue
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            continue
        if _day_iso(dt) == day:
            out.append(row)
    return out


def build_evening_brief(
    entries: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> str:
    """One German paragraph from today's log. Deterministic, no model."""
    today = filter_today(entries, now=now)
    if not today:
        return "Heute noch keine Agent-Tätigkeit."

    skipped = [r for r in today if r.get("action") == ACTION_SKIPPED]
    offline = [
        r
        for r in skipped
        if "offline" in str(r.get("detail") or r.get("result") or "").lower()
    ]
    backups_ok = [
        r
        for r in today
        if r.get("action") == ACTION_APPLY
        and str(r.get("kind") or "") == "backup"
        and str(r.get("result") or "") == RESULT_OK
    ]
    backups_fail = [
        r
        for r in today
        if r.get("action") == ACTION_APPLY
        and str(r.get("kind") or "") == "backup"
        and str(r.get("result") or "") == RESULT_FAIL
    ]
    # Apply rows may use kind patch/image; also count started+result from apply
    patches = [
        r
        for r in today
        if r.get("action") == ACTION_APPLY
        and str(r.get("kind") or "") in ("patch", "image")
        and str(r.get("result") or "") == RESULT_OK
    ]
    images = [r for r in patches if str(r.get("kind") or "") == "image"]
    pkgs = [r for r in patches if str(r.get("kind") or "") == "patch"]
    reboots = [r for r in today if r.get("action") == ACTION_REBOOT]
    warns = [r for r in today if r.get("action") == ACTION_WARN]
    prunes = [
        r
        for r in today
        if r.get("action") == ACTION_PRUNE and str(r.get("result") or "") == RESULT_OK
    ]

    parts: list[str] = []
    if offline:
        parts.append(
            f"{len(offline)} Host(s) offline — Backup/Patch heute ausgelassen, fällt auf"
            + (f" ({_join_de(_names(offline))})" if _names(offline) else "")
        )
    other_skip = [r for r in skipped if r not in offline]
    if other_skip:
        parts.append(
            f"{len(other_skip)} übersprungen"
            + (f" ({_join_de(_names(other_skip))})" if _names(other_skip) else "")
        )
    bak_bits: list[str] = []
    if backups_ok:
        bak_bits.append(f"{len(backups_ok)} Backup(s) ok")
    if backups_fail:
        bak_bits.append(
            f"{len(backups_fail)} Backup(s) fehlgeschlagen"
            + (f" ({_join_de(_names(backups_fail))})" if _names(backups_fail) else "")
        )
    if bak_bits:
        parts.append(" und ".join(bak_bits))
    if pkgs or images:
        img = f", {len(images)} Image(s)" if images else ""
        parts.append(f"{len(pkgs)} Patch(es){img} eingespielt")
    reboot_ok = [r for r in reboots if str(r.get("result") or "") == RESULT_OK]
    reboot_wait = [r for r in reboots if str(r.get("result") or "") == RESULT_WAIT]
    if reboot_ok:
        parts.append(f"{len(reboot_ok)} Reboot(s) {VIA_AGENT}")
    if reboot_wait:
        parts.append(f"{len(reboot_wait)} Reboot(s) warten auf dich")
    if prunes:
        parts.append(f"{len(prunes)} Image-Prune(s) {VIA_AGENT}")
    if warns:
        details = [
            str(r.get("detail") or "").strip()
            for r in warns
            if str(r.get("detail") or "").strip()
        ]
        hint = f" ({details[0][:160]})" if details else ""
        parts.append(f"{len(warns)} Warnung(en){hint}")
    if not parts:
        started = [r for r in today if r.get("action") == ACTION_STARTED]
        planned = [r for r in today if r.get("action") == ACTION_PLANNED]
        if started:
            parts.append(f"{len(started)} Auftrag(e) gestartet")
        elif planned:
            parts.append(f"{len(planned)} Fenster geplant")
        else:
            return "Heute Agent-Tätigkeit, aber noch keine abgeschlossene Abendbilanz."
    text = "Heute Abend: " + "; ".join(parts) + "."
    if not text.endswith("."):
        text += "."
    return text
