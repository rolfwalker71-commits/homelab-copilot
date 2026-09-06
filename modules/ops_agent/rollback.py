"""Decide autonomous pre-apply snapshot rollback. DistUpgrade is never eligible."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.core.compose_apply import is_hub_rate_limit
from ops_agent.actor import actor_fields, by_agent
from ops_agent.image_snaps import snap_from_job_result

ELIGIBLE_KINDS = frozenset({"apply", "image-apply"})

REASON_APPLY_FAILED = "apply_failed"
REASON_TIMEOUT = "timeout"
REASON_UNHEALTHY = "unhealthy"
REASON_RATE_LIMIT = "rate_limit"

REASON_LABELS_DE = {
    REASON_APPLY_FAILED: "Apply fehlgeschlagen",
    REASON_TIMEOUT: "Timeout",
    REASON_UNHEALTHY: "Ungesund nach Apply",
    REASON_RATE_LIMIT: "Docker-Hub-Limit",
}

JOB_KIND_LABELS_DE = {
    "apply": "Patch-Apply",
    "image-apply": "Image-Apply",
    "release-upgrade": "Release-Upgrade",
    "apply-batch": "Stapel-Apply",
}

SKIP_NO_SNAP = "Kein Pre-Apply-Snapshot — Rollback übersprungen."
SKIP_KIND = "Kein autonomes Rollback für diesen Auftragstyp."
SKIP_ALREADY = "Rollback bereits versucht — kein weiterer Anlauf."
SKIP_NO_TARGET = "Kein Ziel für Rollback."
SKIP_RATE_LIMIT = (
    "Docker-Hub-Limit — nichts eingespielt, Snapshot bleibt. Kein Rollback."
)

_SNAP_LOG = re.compile(
    r"Proxmox-Snapshot\s+[„\"]([^\"”]+)[“\"]\s+angelegt",
    re.I,
)


@dataclass(frozen=True)
class RollbackPlan:
    action: str  # rollback | skip
    job_kind: str
    snap_name: str
    reason_code: str
    skip_reason: str


def classify_fail_reason(error: str | None) -> str:
    if is_hub_rate_limit(error):
        return REASON_RATE_LIMIT
    low = (error or "").lower()
    if "timeout" in low or "zeitüberschreitung" in low:
        return REASON_TIMEOUT
    if any(
        token in low
        for token in (
            "unhealthy",
            "ungesund",
            "health-check",
            "health check",
            "nicht erreichbar nach",
        )
    ):
        return REASON_UNHEALTHY
    return REASON_APPLY_FAILED


def reason_label_de(code: str) -> str:
    return REASON_LABELS_DE.get(code, code or REASON_LABELS_DE[REASON_APPLY_FAILED])


def job_kind_label_de(kind: str) -> str:
    return JOB_KIND_LABELS_DE.get(kind, kind or "Apply")


def snap_name_from_logs(lines: list[str] | None) -> str | None:
    """Only snaps this job logged as created — never an older arbitrary name."""
    found: list[str] = []
    for line in lines or []:
        match = _SNAP_LOG.search(str(line))
        if match:
            name = match.group(1).strip()
            if name:
                found.append(name)
    if not found:
        return None
    return found[-1]


def snap_name_for_job(
    target_id: str,
    result: dict[str, Any] | None,
    log_lines: list[str] | None = None,
) -> str | None:
    created = snap_from_job_result(target_id, result)
    if created and created.name:
        return created.name
    return snap_name_from_logs(log_lines)


def plan_rollback(
    *,
    job_kind: str,
    target_id: str,
    result: dict[str, Any] | None,
    error: str | None,
    log_lines: list[str] | None = None,
    already: bool = False,
) -> RollbackPlan:
    reason = classify_fail_reason(error)
    kind = str(job_kind or "").strip()
    if already:
        return RollbackPlan("skip", kind, "", reason, SKIP_ALREADY)
    if reason == REASON_RATE_LIMIT:
        return RollbackPlan("skip", kind, "", reason, SKIP_RATE_LIMIT)
    if kind not in ELIGIBLE_KINDS:
        return RollbackPlan("skip", kind, "", reason, SKIP_KIND)
    tid = str(target_id or "").strip()
    if not tid:
        return RollbackPlan("skip", kind, "", reason, SKIP_NO_TARGET)
    snap = snap_name_for_job(tid, result, log_lines)
    if not snap:
        return RollbackPlan("skip", kind, "", reason, SKIP_NO_SNAP)
    return RollbackPlan("rollback", kind, snap, reason, "")


def window_reason_after_rollback(apply_error: str | None, rec: dict[str, Any]) -> str:
    apply_txt = (apply_error or "Apply fehlgeschlagen.").strip()
    if apply_txt.endswith("."):
        apply_txt = apply_txt[:-1]
    status = str(rec.get("status") or "")
    snap = str(rec.get("snap_name") or "").strip()
    label = reason_label_de(str(rec.get("reason") or ""))
    if status == "ok":
        body = (
            f"{apply_txt}. Zurückgesetzt auf Snapshot „{snap}“ ({label}). "
            "Welle gestoppt."
        )
    elif status == "failed":
        err = str(rec.get("error") or "unbekannt").strip()
        body = (
            f"{apply_txt}. Rollback auf „{snap}“ fehlgeschlagen: {err} "
            "Welle gestoppt."
        )
    else:
        skip = str(rec.get("error") or SKIP_NO_SNAP).strip()
        body = f"{apply_txt}. {skip} Welle gestoppt."
    return by_agent(body)


def rollback_audit_fields() -> dict[str, Any]:
    return actor_fields(via_agent=True)
