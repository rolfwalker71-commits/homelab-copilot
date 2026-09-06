"""Local apply lessons — rule-based, no LLM, no USN crawler.

Scans today store name/current/candidate/priority/meta only. There is no apt
changelog, NEWS, or USN text on the package rows. Do not fetch bulletins.
If a future scan already puts changelog/news/breaks on the package dict (or
meta), surface one line for packages in *this* job. reboot_required from the
existing scan flag is the only cheap note available today.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ops_agent.actor import VIA_AGENT, actor_fields, by_agent

ERROR_DISK = "disk"
ERROR_DPKG_LOCK = "dpkg_lock"
ERROR_TIMEOUT = "timeout"
ERROR_NETWORK = "network"
ERROR_APT_CONFLICT = "apt_conflict"
ERROR_COMPOSE = "compose"
ERROR_UNHEALTHY = "unhealthy"
ERROR_APPLY = "apply_failed"

HOST_LXC = "lxc"
HOST_QEMU = "qemu"
HOST_MANUAL = "manual"
HOST_HOST = "host"

JOB_PATCH = "patch"
JOB_IMAGE = "image"

WHY_DE = {
    ERROR_DISK: "Disk voll oder kritisch — Apply abgebrochen.",
    ERROR_DPKG_LOCK: "dpkg-Sperre — ein anderer apt-Prozess lief noch.",
    ERROR_TIMEOUT: "Timeout — Host hat nicht rechtzeitig geantwortet.",
    ERROR_NETWORK: "Netz/SSH-Fehler — Verbindung abgebrochen.",
    ERROR_APT_CONFLICT: "apt-Konflikt oder kaputte Abhängigkeiten.",
    ERROR_COMPOSE: "Compose- oder Image-Pull fehlgeschlagen.",
    ERROR_UNHEALTHY: "Host ungesund nach Apply.",
    ERROR_APPLY: "Apply fehlgeschlagen.",
}

HOST_LABEL_DE = {
    HOST_LXC: "LXC",
    HOST_QEMU: "VM",
    HOST_MANUAL: "Manuell",
    HOST_HOST: "Host",
}

JOB_LABEL_DE = {
    JOB_PATCH: "Patch",
    JOB_IMAGE: "Images",
}

# One timeout/network blip must not ban a package. Rollback is a strong signal.
_HOLD_AFTER = 2


@dataclass(frozen=True)
class LessonHold:
    reason: str
    error_class: str
    why_de: str
    lesson_id: int | None = None


def host_kind_of(target_id: str) -> str:
    raw = str(target_id or "").strip().lower()
    if raw.startswith("lxc:"):
        return HOST_LXC
    if raw.startswith("qemu:"):
        return HOST_QEMU
    if raw.startswith("manual:"):
        return HOST_MANUAL
    return HOST_HOST


def job_kind_of(*, kind: str = "", bucket: str = "") -> str:
    if str(bucket or "").strip().lower() == "images" or str(kind or "").strip().lower() == "image":
        return JOB_IMAGE
    if str(kind or "").strip().lower() in ("image-apply", "images"):
        return JOB_IMAGE
    return JOB_PATCH


def package_names(raw: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("package") or "").strip()
        else:
            name = str(item or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def packages_key(names: list[str], *, bucket: str = "") -> str:
    cleaned = sorted({n.strip().lower() for n in names if str(n).strip()})
    if cleaned:
        return ",".join(cleaned)
    return f"bucket:{(bucket or 'patch').strip().lower() or 'patch'}"


def classify_error_class(error: str | None) -> str:
    low = (error or "").lower()
    if any(t in low for t in ("no space", "disk voll", "disk ist kritisch", "enospc")):
        return ERROR_DISK
    if any(t in low for t in ("dpkg lock", "unable to lock", "could not get lock", "/var/lib/dpkg")):
        return ERROR_DPKG_LOCK
    if any(t in low for t in ("timeout", "zeitüberschreitung", "timed out")):
        return ERROR_TIMEOUT
    if "offline" in low or "nicht online" in low:
        return ERROR_NETWORK
    if any(
        t in low
        for t in (
            "connection refused",
            "network is unreachable",
            "no route to host",
            "broken pipe",
            "connection reset",
            "ssh: connect",
        )
    ):
        return ERROR_NETWORK
    if any(
        t in low
        for t in (
            "unmet dependencies",
            "held broken packages",
            "conflict",
            "dpkg: error",
            "unable to correct",
        )
    ):
        return ERROR_APT_CONFLICT
    if any(
        t in low
        for t in ("compose", "image pull", "pull failed", "manifest unknown", "docker compose")
    ):
        return ERROR_COMPOSE
    if any(
        t in low
        for t in ("unhealthy", "ungesund", "health-check", "health check", "nicht erreichbar nach")
    ):
        return ERROR_UNHEALTHY
    return ERROR_APPLY


def why_de(error_class: str) -> str:
    return WHY_DE.get(error_class, WHY_DE[ERROR_APPLY])


def error_short(error: str | None, *, limit: int = 160) -> str:
    text = " ".join((error or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def next_action_de(*, host_kind: str, job_kind: str) -> str:
    host = HOST_LABEL_DE.get(host_kind, host_kind or "Host")
    job = JOB_LABEL_DE.get(job_kind, "Patch")
    return f"Nächstes Mal: gleiches {job}-Set auf {host} nicht blind wiederholen — wartet auf dich."


def hold_reason_de(lesson: dict[str, Any]) -> str:
    why = str(lesson.get("why_de") or why_de(str(lesson.get("error_class") or "")))
    nxt = str(lesson.get("next_de") or "").strip()
    body = f"Übersprungen: letzte Lektion — {why.rstrip('.')}"
    if nxt:
        body = f"{body}. {nxt}"
    return by_agent(body)


def should_hold(
    lessons: list[dict[str, Any]],
    *,
    packages_key: str,
    host_kind: str,
    job_kind: str,
) -> LessonHold | None:
    """Hold if a rollback hit this combo, or two similar fails on a comparable host."""
    wanted_key = str(packages_key or "")
    wanted_host = str(host_kind or "")
    wanted_job = str(job_kind or "")
    matches = [
        row
        for row in lessons
        if str(row.get("packages_key") or "") == wanted_key
        and str(row.get("host_kind") or "") == wanted_host
        and str(row.get("job_kind") or "") == wanted_job
    ]
    if not matches:
        return None
    by_class: dict[str, list[dict[str, Any]]] = {}
    for row in matches:
        by_class.setdefault(str(row.get("error_class") or ERROR_APPLY), []).append(row)
    chosen: dict[str, Any] | None = None
    for _cls, group in by_class.items():
        if any(bool(r.get("rollback_ran")) for r in group):
            chosen = next(r for r in reversed(group) if r.get("rollback_ran"))
            break
        if len(group) >= _HOLD_AFTER:
            chosen = group[-1]
            break
    if chosen is None:
        return None
    lid = chosen.get("id")
    return LessonHold(
        reason=hold_reason_de(chosen),
        error_class=str(chosen.get("error_class") or ERROR_APPLY),
        why_de=str(chosen.get("why_de") or why_de(str(chosen.get("error_class") or ""))),
        lesson_id=int(lid) if lid is not None else None,
    )


def scan_apply_note(
    *,
    job_packages: list[str],
    reboot_required: bool = False,
    scan_packages: list[dict[str, Any]] | None = None,
) -> str | None:
    """One-line note from data already on the scan. Never fetches USN/NEWS."""
    bits: list[str] = []
    if reboot_required:
        bits.append("Reboot nötig")
    wanted = {n.strip().lower() for n in job_packages if str(n).strip()}
    if wanted and scan_packages:
        for pkg in scan_packages:
            name = str(pkg.get("name") or "").strip().lower()
            if name not in wanted:
                continue
            line = _pkg_local_note(pkg)
            if line and line not in bits:
                bits.append(line)
                if len(bits) >= 3:
                    break
    return " · ".join(bits) if bits else None


def _pkg_local_note(pkg: dict[str, Any]) -> str | None:
    meta = pkg.get("meta") if isinstance(pkg.get("meta"), dict) else {}
    breaks = pkg.get("breaks") or meta.get("breaks")
    if breaks:
        return f"Breaks {_clip(breaks, 60)}"
    for key in ("changelog", "news"):
        raw = pkg.get(key) or meta.get(key)
        if raw:
            return _clip(raw, 80)
    return None


def _clip(raw: Any, limit: int) -> str:
    text = " ".join(str(raw).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def serialize_lesson(row: dict[str, Any]) -> dict[str, Any]:
    host_kind = str(row.get("host_kind") or "")
    job_kind = str(row.get("job_kind") or "")
    out = dict(row)
    out["event"] = "lesson"
    out["kind"] = "lesson"
    out["kind_label"] = "Lektion"
    out["host_kind_label"] = HOST_LABEL_DE.get(host_kind, host_kind or "Host")
    out["job_kind_label"] = JOB_LABEL_DE.get(job_kind, job_kind or "Patch")
    out["error_label"] = why_de(str(row.get("error_class") or ""))
    out["via_agent"] = bool(row.get("via_agent", True))
    out.update(actor_fields(via_agent=True))
    if VIA_AGENT.lower() not in str(out.get("why_de") or "").lower():
        out["why_de"] = by_agent(str(out.get("why_de") or why_de(str(row.get("error_class") or ""))))
    return out
