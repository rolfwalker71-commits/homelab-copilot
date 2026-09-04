"""System crontab sync for patcher scan schedules."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Any

from patcher.config import PatcherSettings, get_patcher_settings

logger = logging.getLogger(__name__)

MARKER_BEGIN = "# --- HOMELAB-COPILOT-PATCHER BEGIN ---"
MARKER_END = "# --- HOMELAB-COPILOT-PATCHER END ---"

_CRON_RE = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$")


class CronError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def preset_to_cron(preset: str, time_hhmm: str = "04:00", weekday: int = 0) -> str:
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
            "(z. B. 0 4 * * *)."
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


def _curl_line(settings: PatcherSettings, target_id: str) -> str:
    base = settings.patcher_api_base.rstrip("/")
    tid = target_id.replace('"', "").replace("'", "")
    payload = f'{{"target_id":"{tid}","wait":false}}'
    url = f"{base}/api/modules/patcher/scan"
    return (
        f"curl -fsS -X POST {url} "
        f"-H 'Content-Type: application/json' "
        f"-d '{payload}' "
        f">> /tmp/homelab-patcher-cron.log 2>&1"
    )


def build_block(
    schedules: list[dict[str, Any]],
    settings: PatcherSettings | None = None,
) -> str:
    settings = settings or get_patcher_settings()
    lines = [
        MARKER_BEGIN,
        "# Managed by Homelab Copilot patcher — do not edit by hand",
    ]
    for s in schedules:
        if not s.get("enabled"):
            continue
        expr = s["cron_expr"]
        target_id = s["target_id"]
        note = (s.get("note") or "").strip()
        if note:
            lines.append(f"# {note}")
        lines.append(f"{expr} {_curl_line(settings, target_id)}")
    lines.append(MARKER_END)
    return "\n".join(lines) + "\n"


def sync_crontab(schedules: list[dict[str, Any]]) -> dict[str, Any]:
    if not crontab_available():
        return {"ok": False, "error": "crontab nicht verfügbar auf diesem Host."}
    try:
        current = _read_crontab()
        block = build_block(schedules)
        # Remove old managed block
        pattern = re.compile(
            re.escape(MARKER_BEGIN) + r".*?" + re.escape(MARKER_END) + r"\n?",
            re.S,
        )
        cleaned = pattern.sub("", current).rstrip() + "\n"
        enabled = [s for s in schedules if s.get("enabled")]
        if enabled:
            new_content = cleaned + "\n" + block
        else:
            new_content = cleaned
        _write_crontab(new_content)
        return {"ok": True, "entries": len(enabled)}
    except CronError as exc:
        logger.warning("patcher crontab sync: %s", exc.message)
        return {"ok": False, "error": exc.message}
