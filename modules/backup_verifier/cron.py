"""Legacy host-crontab helpers (presets + optional leftover-block cleanup).

Schedules run in-process (see scheduler.py). Host crontab curls are unused and
would fail TOTP. If ``crontab`` exists in this process, sync strips the old
marker-managed curl block so leftover entries cannot double-fire later.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Any

from backup_verifier.config import BackupSettings, get_backup_settings

logger = logging.getLogger(__name__)

MARKER_BEGIN = "# --- HOMELAB-COPILOT-BACKUP-VERIFIER BEGIN ---"
MARKER_END = "# --- HOMELAB-COPILOT-BACKUP-VERIFIER END ---"

_CRON_RE = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$")


class CronError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def preset_to_cron(preset: str, time_hhmm: str = "03:00", weekday: int = 0) -> str:
    """Map UI presets to cron expressions (Europe/Berlin wall clock)."""
    preset = (preset or "custom").lower()
    try:
        hour_s, minute_s = time_hhmm.split(":")
        hour, minute = int(hour_s), int(minute_s)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError as exc:
        raise CronError("Ungültige Uhrzeit — erwartet HH:MM") from exc

    if preset == "daily":
        return f"{minute} {hour} * * *"
    if preset == "weekly":
        wd = max(0, min(6, int(weekday)))
        return f"{minute} {hour} * * {wd}"
    raise CronError(f"Unbekanntes Preset: {preset}")


def validate_cron_expr(expr: str) -> str:
    expr = " ".join((expr or "").split())
    if not _CRON_RE.match(expr):
        raise CronError(
            "Ungültiger Cron-Ausdruck — erwartet: „m h dom mon dow“ "
            "(z. B. 0 3 * * *)."
        )
    return expr


def crontab_available() -> bool:
    return shutil.which("crontab") is not None


def _read_crontab() -> str:
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise CronError(f"crontab nicht ausführbar: {exc}") from exc
    if result.returncode != 0:
        err = (result.stderr or "").lower()
        if "no crontab" in err or result.returncode == 1:
            return ""
        raise CronError(f"crontab -l fehlgeschlagen: {result.stderr.strip()}")
    return result.stdout or ""


def _write_crontab(content: str) -> None:
    result = subprocess.run(
        ["crontab", "-"],
        input=content,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CronError(f"crontab schreiben fehlgeschlagen: {result.stderr.strip()}")


def _strip_marker_block(current: str) -> str:
    pattern = re.compile(
        re.escape(MARKER_BEGIN) + r".*?" + re.escape(MARKER_END) + r"\n?",
        re.DOTALL,
    )
    return pattern.sub("", current).rstrip() + "\n"


def build_block(schedules: list[dict[str, Any]], bsettings: BackupSettings | None = None) -> str:
    """Comment-only leftover notice — no curl lines (TOTP would reject them)."""
    del bsettings
    enabled = [s for s in schedules if s.get("enabled")]
    lines = [
        MARKER_BEGIN,
        "# Managed by Homelab Copilot backup_verifier — do not edit by hand",
        "# Zeitpläne laufen in der App (Europe/Berlin). Kein Host-Cron / curl nötig.",
        "# Diesen Block kannst du aus der Host-Crontab löschen.",
    ]
    for s in enabled:
        note = (s.get("note") or "").strip()
        extra = f"  # {note}" if note else ""
        lines.append(
            f"# {s.get('cron_expr')}  {s.get('stack')}  {s.get('parent_id')}{extra}"
        )
    lines.append(MARKER_END)
    return "\n".join(lines) + "\n"


def sync_crontab(schedules: list[dict[str, Any]], bsettings: BackupSettings | None = None) -> dict[str, Any]:
    """Strip leftover curl marker blocks when this process can write crontab."""
    bsettings = bsettings or get_backup_settings()
    block = build_block(schedules, bsettings)
    if not crontab_available():
        return {
            "ok": True,
            "synced": False,
            "scheduler": "in_process",
            "reason": (
                "Kein crontab in diesem Prozess (typisch im Container). "
                "Zeitpläne laufen in der App — Host-Crontab nicht nötig."
            ),
            "block": block,
            "hint": (
                "Alten Marker-Block auf dem Docker-Host optional löschen "
                "(crontab -e). curl ohne TOTP-Cookie schlägt mit 401 fehl."
            ),
        }

    current = _read_crontab()
    cleaned = _strip_marker_block(current)
    enabled = [s for s in schedules if s.get("enabled")]
    if enabled:
        new_content = cleaned + "\n" + block
    else:
        new_content = cleaned
        block = ""

    _write_crontab(new_content)
    return {
        "ok": True,
        "synced": True,
        "scheduler": "in_process",
        "entries": len(enabled),
        "block": block,
        "user": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
    }


def preview_crontab(
    schedules: list[dict[str, Any]], bsettings: BackupSettings | None = None
) -> str:
    del bsettings
    enabled = [s for s in schedules if s.get("enabled")]
    lines = [
        "In-App-Scheduler (Europe/Berlin) — kein Host-Crontab nötig.",
        "",
    ]
    if not enabled:
        lines.append("Keine aktiven Zeitpläne.")
        return "\n".join(lines) + "\n"
    for s in enabled:
        lines.append(f"{s.get('cron_expr')}  {s.get('stack')}  ({s.get('parent_id')})")
    return "\n".join(lines) + "\n"
