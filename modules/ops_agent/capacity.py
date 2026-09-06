"""Proactive dest capacity from backup history + existing dest probes.

Never invents quota. Unknown free space → no dest skip (guest-disk gate remains).
"""

from __future__ import annotations

from typing import Any

CAPACITY_WARN_AFTER_PCT = 80.0
CAPACITY_BLOCK_PCT = 95.0
LOOKBACK_OK = 5

DestUsage = dict[str, Any]


def estimate_bytes_from_runs(runs: list[dict[str, Any]]) -> int | None:
    """Median of recent successful backup sizes. None if no history."""
    sizes: list[int] = []
    for row in runs:
        status = str(row.get("status") or "")
        if status not in ("success", "partial", "ok"):
            continue
        raw = row.get("size_bytes")
        if raw is None:
            raw = row.get("bytes_added")
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n > 0:
            sizes.append(n)
        if len(sizes) >= LOOKBACK_OK:
            break
    if not sizes:
        return None
    sizes.sort()
    return sizes[len(sizes) // 2]


def _int(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def dest_free_total(usage: DestUsage | None) -> tuple[int | None, int | None, float | None]:
    if not isinstance(usage, dict) or not usage.get("quota_known"):
        return None, None, None
    free = _int(usage.get("free_bytes"))
    total = _int(usage.get("total_bytes"))
    used = _int(usage.get("used_bytes"))
    pct = usage.get("used_pct")
    try:
        used_pct = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        used_pct = None
    if used_pct is None and used is not None and total:
        used_pct = 100.0 * used / total
    return free, total, used_pct


def job_fits(free_bytes: int | None, estimate: int | None) -> bool | None:
    """True/False when both known; None = do not skip for dest."""
    if free_bytes is None or estimate is None:
        return None
    return estimate <= free_bytes


def projected_fill_pct(used_bytes: int | None, total_bytes: int | None, add_bytes: int) -> float | None:
    if used_bytes is None or not total_bytes:
        return None
    return 100.0 * max(0, used_bytes + max(0, add_bytes)) / total_bytes


def dest_is_critically_full(usage: DestUsage | None, *, estimate: int | None = None) -> bool:
    free, _total, pct = dest_free_total(usage)
    if pct is not None and pct >= CAPACITY_BLOCK_PCT:
        return True
    if free is not None and estimate is not None and estimate > free:
        return True
    return False


def dest_is_tight(
    usage: DestUsage | None,
    *,
    upcoming_bytes: int,
    next_job_bytes: int | None = None,
    next_n: int = 3,
) -> bool:
    free, total, pct = dest_free_total(usage)
    used = _int((usage or {}).get("used_bytes"))
    after = projected_fill_pct(used, total, upcoming_bytes)
    if after is not None and after >= CAPACITY_WARN_AFTER_PCT:
        return True
    if free is not None and upcoming_bytes > 0 and upcoming_bytes > free:
        return True
    if free is not None and next_job_bytes and next_n > 0:
        need = next_job_bytes * next_n
        if need > free:
            return True
    if pct is not None and pct >= CAPACITY_WARN_AFTER_PCT and upcoming_bytes > 0:
        return True
    return False


def collect_dests(payload: dict[str, Any] | None) -> list[tuple[str, DestUsage]]:
    """Named dests from build_backup_storage() / test doubles."""
    if not isinstance(payload, dict):
        return []
    out: list[tuple[str, DestUsage]] = []
    for key, label in (("hetzner", "Hetzner"), ("copilot", "Copilot"), ("volume", "Volume")):
        row = payload.get(key)
        if isinstance(row, dict):
            copy = dict(row)
            copy.setdefault("label", row.get("label") or label)
            out.append((str(copy.get("label") or label), copy))
    return out


def warn_lines(
    dests: list[tuple[str, DestUsage]],
    *,
    upcoming_bytes: int,
    next_job_bytes: int | None = None,
) -> list[str]:
    lines: list[str] = []
    for label, usage in dests:
        if not dest_is_tight(
            usage, upcoming_bytes=upcoming_bytes, next_job_bytes=next_job_bytes
        ):
            continue
        free, total, _pct = dest_free_total(usage)
        after = projected_fill_pct(_int(usage.get("used_bytes")), total, upcoming_bytes)
        after_s = f"{after:.0f} %" if after is not None else "unbekannt"
        lines.append(
            f"{label}: nach der Kette etwa {after_s} belegt"
            + (f" (frei jetzt {free} Bytes)" if free is not None else "")
            + " — knapper Speicher, Jobs die nicht passen werden ausgelassen."
        )
    return lines
