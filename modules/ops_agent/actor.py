"""Mark work the ops agent did, so the UI can tell agent vs manual."""

from __future__ import annotations

from typing import Any

ACTOR_AGENT = "Agent"
VIA_AGENT = "durch Agent"

_PHRASES = {
    "patches_applied": "Patches eingespielt",
    "images_applied": "Image-Update eingespielt",
    "snap_before": "Snapshot vor Apply",
    "snap_deleted": "Snapshot nach Erfolg entfernt",
    "rolled_back": "Zurückgesetzt auf Snapshot „{snap}“",
    "rollback_failed": "Rollback auf „{snap}“ fehlgeschlagen",
    "rollback_skipped": "Rollback übersprungen",
    "window_planned": "Fenster geplant",
    "window_shifted": "Fenster verschoben",
    "backup_started": "Backup gestartet",
    "wave_stopped": "Welle gestoppt nach Apply-Fehler",
}


def actor_fields(*, via_agent: bool) -> dict[str, Any]:
    flag = bool(via_agent)
    return {
        "via_agent": flag,
        "actor": ACTOR_AGENT if flag else "",
        "actor_label": VIA_AGENT if flag else "",
    }


def is_via_agent(value: Any) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, dict):
        if value.get("via_agent") or value.get("actor") == ACTOR_AGENT:
            return True
        src = str(value.get("source") or "").strip().lower()
        return src == "agent"
    return str(getattr(value, "via_agent", False) or "").lower() in {"1", "true"}


def by_agent(text: str, *, via_agent: bool = True) -> str:
    """Suffix a German sentence with „durch Agent“ once. Manual stays unchanged."""
    raw = " ".join((text or "").split())
    if not via_agent:
        return raw
    if VIA_AGENT.lower() in raw.lower():
        return raw
    if not raw:
        return f"{_PHRASES['window_planned']} {VIA_AGENT}."
    if raw.endswith("."):
        return f"{raw[:-1]} {VIA_AGENT}."
    return f"{raw} {VIA_AGENT}"


def agent_phrase(key: str, *, via_agent: bool = True, snap: str = "") -> str:
    base = _PHRASES.get(key, key)
    if "{snap}" in base:
        base = base.format(snap=snap or "—")
    return by_agent(base, via_agent=via_agent)
