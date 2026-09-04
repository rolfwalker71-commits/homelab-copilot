"""System crontab sync for backup schedules (marker-managed block)."""

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

_CRON_RE = re.compile(
    r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$"
)


class CronError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def preset_to_cron(preset: str, time_hhmm: str = "03:00", weekday: int = 0) -> str:
    """Map UI presets to cron expressions (Europe/Berlin wall clock via host TZ)."""
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
    # exit 1 with empty often means no crontab
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


def _curl_line(bsettings: BackupSettings, parent_id: str, stack: str) -> str:
    base = bsettings.backup_api_base.rstrip("/")
    # Escape for single-quoted shell JSON
    payload = (
        '{"parent_id":"%s","project":"%s"}'
        % (parent_id.replace('"', ""), stack.replace('"', ""))
    )
    url = f"{base}/api/modules/backup_verifier/run"
    return (
        f"curl -fsS -X POST {url} "
        f"-H 'Content-Type: application/json' "
        f"-d '{payload}' "
        f">> /tmp/homelab-backup-verifier-cron.log 2>&1"
    )


def build_block(schedules: list[dict[str, Any]], bsettings: BackupSettings | None = None) -> str:
    bsettings = bsettings or get_backup_settings()
    lines = [MARKER_BEGIN, "# Managed by Homelab Copilot backup_verifier — do not edit by hand"]
    for s in schedules:
        if not s.get("enabled"):
            continue
        expr = s["cron_expr"]
        parent_id = s["parent_id"]
        stack = s["stack"]
        note = (s.get("note") or "").strip()
        if note:
            lines.append(f"# {note}")
        lines.append(f"{expr} {_curl_line(bsettings, parent_id, stack)}")
    lines.append(MARKER_END)
    return "\n".join(lines) + "\n"


def sync_crontab(schedules: list[dict[str, Any]], bsettings: BackupSettings | None = None) -> dict[str, Any]:
    """Replace marker block in user crontab with current schedules."""
    bsettings = bsettings or get_backup_settings()
    if not crontab_available():
        block = build_block(schedules, bsettings)
        return {
            "ok": False,
            "synced": False,
            "reason": (
                "Kein crontab auf diesem Host (typisch im Container). "
                "Schedules sind in der DB gespeichert — Block manuell auf dem "
                "Copilot-Host installieren."
            ),
            "block": block,
            "hint": "crontab -e → Block einfügen, oder Host-Cron mit curl auf BACKUP_API_BASE",
        }

    current = _read_crontab()
    # Strip existing block
    pattern = re.compile(
        re.escape(MARKER_BEGIN) + r".*?" + re.escape(MARKER_END) + r"\n?",
        re.DOTALL,
    )
    cleaned = pattern.sub("", current).rstrip() + "\n"
    enabled = [s for s in schedules if s.get("enabled")]
    if enabled:
        block = build_block(schedules, bsettings)
        new_content = cleaned + "\n" + block
    else:
        new_content = cleaned
        block = ""

    _write_crontab(new_content)
    return {
        "ok": True,
        "synced": True,
        "entries": len(enabled),
        "block": block,
        "user": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
    }


def preview_crontab(
    schedules: list[dict[str, Any]], bsettings: BackupSettings | None = None
) -> str:
    return build_block(schedules, bsettings or get_backup_settings())
