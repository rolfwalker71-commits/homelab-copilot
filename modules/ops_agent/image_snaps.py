"""Pre-image snapshot bookkeeping — delete only after success, before the next target."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ImageSnap:
    target_id: str
    name: str


def snap_to_delete_before_next(last_ok: ImageSnap | None) -> ImageSnap | None:
    """Successful pre-image snap is removed immediately before the next image target."""
    return last_ok


def remember_after_image(
    last_ok: ImageSnap | None,
    *,
    ok: bool,
    created: ImageSnap | None,
) -> ImageSnap | None:
    """Success queues the new snap for later delete. Failure leaves it (and last_ok) alone."""
    if ok:
        return created
    return last_ok


def snap_from_job_result(target_id: str, result: dict | None) -> ImageSnap | None:
    """Read the name written by maybe_pre_snapshot into an image-apply job result."""
    if not isinstance(result, dict):
        return None
    info = result.get("snapshot")
    if not isinstance(info, dict):
        return None
    if info.get("skipped"):
        return None
    name = str(info.get("name") or "").strip()
    if not name:
        return None
    tid = str(target_id or "").strip()
    if not tid:
        return None
    return ImageSnap(target_id=tid, name=name)
