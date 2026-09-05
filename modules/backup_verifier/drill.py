"""Restore-drill evaluation (pure): restic check / cheap tar list → pass/fail."""

from __future__ import annotations

from typing import Any

ENGINE_TAR = "tar"
ENGINE_RESTIC = "restic"

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


def evaluate_restic_check(
    *,
    exit_code: int | None,
    stderr: str = "",
    stdout: str = "",
) -> dict[str, Any]:
    """Pass only when restic check exits 0."""
    code = 0 if exit_code is None else int(exit_code)
    blob = f"{stdout or ''}\n{stderr or ''}".strip()
    if code == 0:
        return {
            "ok": True,
            "status": STATUS_SUCCESS,
            "detail": "restic check OK",
        }
    err = blob.replace("\n", " ").strip() or f"exit {code}"
    if len(err) > 240:
        err = err[:239].rstrip() + "…"
    return {"ok": False, "status": STATUS_FAILED, "detail": f"restic check: {err}"}


def evaluate_tar_list(
    *,
    readable: bool,
    member_count: int | None = None,
    error: str | None = None,
    downloaded: bool = False,
) -> dict[str, Any]:
    """Cheap tar drill: archive listable. Never require a full download."""
    if downloaded:
        return {
            "ok": False,
            "status": STATUS_SKIPPED,
            "detail": "Tar-Drill übersprungen — Archiv würde vollständig geladen.",
        }
    if error:
        msg = str(error).strip()[:240]
        return {"ok": False, "status": STATUS_FAILED, "detail": msg}
    if not readable:
        return {
            "ok": False,
            "status": STATUS_FAILED,
            "detail": "Archiv nicht lesbar / Liste fehlgeschlagen.",
        }
    n = int(member_count) if member_count is not None else 0
    if n <= 0:
        return {
            "ok": False,
            "status": STATUS_FAILED,
            "detail": "Archiv leer oder ohne Einträge.",
        }
    return {
        "ok": True,
        "status": STATUS_SUCCESS,
        "detail": f"tar-Liste OK ({n} Einträge)",
    }


def should_push_drill(prev_status: str | None, new_status: str) -> str | None:
    """Push only on failure, or first success after a stored failure.

    Returns ``fail`` / ``recovered`` / ``None``.
    """
    prev = (prev_status or "").strip().lower() or None
    nxt = (new_status or "").strip().lower()
    if nxt == STATUS_FAILED:
        return "fail"
    if nxt == STATUS_SUCCESS and prev == STATUS_FAILED:
        return "recovered"
    return None


def summarize_drill_batch(results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [r for r in results if r.get("status") == STATUS_FAILED]
    skipped = [r for r in results if r.get("status") == STATUS_SKIPPED]
    ok = [r for r in results if r.get("status") == STATUS_SUCCESS]
    if failed:
        overall = STATUS_FAILED
    elif ok or skipped:
        overall = STATUS_SUCCESS
    else:
        overall = STATUS_SKIPPED
    return {
        "status": overall,
        "ok_count": len(ok),
        "fail_count": len(failed),
        "skip_count": len(skipped),
        "total": len(results),
    }
