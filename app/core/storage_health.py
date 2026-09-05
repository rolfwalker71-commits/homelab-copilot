"""Storage fill projection + sample downsample (pure helpers)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Keep SQLite small: hourly for ~2 days, then daily, hard cap.
MAX_HOURLY = 48
MAX_DAILY = 60
MIN_RATE_BYTES_PER_DAY = 1.0
MIN_SPAN_SECONDS = 3600.0


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fill_projection(
    samples: list[dict[str, Any]] | None,
    *,
    used: float | None = None,
    total: float | None = None,
    rate_per_day: float | None = None,
) -> dict[str, Any] | None:
    """Return ``{days, label}`` only when data supports it — never invent.

    Needs two+ timed samples with increasing used, or an explicit used+rate.
    """
    used_now = _as_float(used)
    total_now = _as_float(total)
    rate = _as_float(rate_per_day)

    points: list[tuple[float, float]] = []
    for row in samples or []:
        if not isinstance(row, dict):
            continue
        ts = _as_ts(row.get("ts") or row.get("sampled_at_epoch") or row.get("t"))
        u = _as_float(row.get("used") or row.get("used_bytes"))
        if ts is None or u is None:
            continue
        points.append((ts, u))
    points.sort(key=lambda p: p[0])

    if used_now is None and points:
        used_now = points[-1][1]
    if total_now is None:
        for row in reversed(samples or []):
            if isinstance(row, dict):
                total_now = _as_float(row.get("total") or row.get("total_bytes"))
                if total_now is not None:
                    break

    if rate is None and len(points) >= 2:
        first_ts, first_used = points[0]
        last_ts, last_used = points[-1]
        span = last_ts - first_ts
        delta = last_used - first_used
        if span >= MIN_SPAN_SECONDS and delta > 0:
            rate = delta / (span / 86400.0)
            if used_now is None:
                used_now = last_used

    if rate is None or rate < MIN_RATE_BYTES_PER_DAY:
        return None
    if used_now is None or total_now is None or total_now <= 0:
        return None
    remaining = total_now - used_now
    if remaining <= 0:
        return {"days": 0, "label": "bereits voll", "rate_per_day": round(rate, 1)}
    days = remaining / rate
    if days > 3650:
        return None
    rounded = int(round(days))
    if rounded < 1:
        label = "in <1d voll"
    else:
        label = f"in ~{rounded}d voll"
    return {
        "days": rounded,
        "label": label,
        "rate_per_day": round(rate, 1),
    }


def downsample_samples(
    samples: list[dict[str, Any]] | None,
    *,
    now_epoch: float | None = None,
    max_hourly: int = MAX_HOURLY,
    max_daily: int = MAX_DAILY,
) -> list[dict[str, Any]]:
    """Keep recent hourly points + older daily buckets. Deterministic, small."""
    rows: list[dict[str, Any]] = []
    for row in samples or []:
        if not isinstance(row, dict):
            continue
        ts = _as_ts(row.get("ts") or row.get("sampled_at_epoch") or row.get("t"))
        if ts is None:
            continue
        copy = dict(row)
        copy["ts"] = ts
        rows.append(copy)
    rows.sort(key=lambda r: float(r["ts"]))
    if not rows:
        return []
    now = now_epoch if now_epoch is not None else float(rows[-1]["ts"])
    hourly_cut = now - (max_hourly * 3600)
    hourly: list[dict[str, Any]] = []
    daily_best: dict[str, dict[str, Any]] = {}
    for row in rows:
        ts = float(row["ts"])
        if ts >= hourly_cut:
            bucket = int(ts // 3600)
            if hourly and int(float(hourly[-1]["ts"]) // 3600) == bucket:
                hourly[-1] = row
            else:
                hourly.append(row)
            if len(hourly) > max_hourly:
                hourly = hourly[-max_hourly:]
        else:
            day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            prev = daily_best.get(day)
            if prev is None or float(row["ts"]) >= float(prev["ts"]):
                daily_best[day] = row
    daily = sorted(daily_best.values(), key=lambda r: float(r["ts"]))
    if len(daily) > max_daily:
        daily = daily[-max_daily:]
    merged = daily + hourly
    merged.sort(key=lambda r: float(r["ts"]))
    return merged


def chip_level_from_pct(pct: float | None) -> str:
    """Match topology gauges: danger ≥90, warn ≥70, else ok."""
    if pct is None:
        return "unknown"
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return "unknown"
    if p >= 90:
        return "danger"
    if p >= 70:
        return "warn"
    return "ok"


def smart_chip(health: str | None, *, failing: bool = False, prefail: bool = False) -> str:
    raw = (health or "").strip().lower()
    if failing or raw in {"failed", "failing", "fail"}:
        return "danger"
    if prefail or raw in {"prefail", "pre-fail", "warning", "warn"}:
        return "warn"
    if raw in {"passed", "ok", "good", "healthy"}:
        return "ok"
    return "unknown"


def zfs_chip(health: str | None) -> str:
    raw = (health or "").strip().upper()
    if raw in {"ONLINE", "OK"}:
        return "ok"
    if raw in {"DEGRADED", "DEGRADED-WAIT", "FAULTED", "UNAVAIL", "OFFLINE", "REMOVED"}:
        return "danger" if raw != "DEGRADED" else "warn"
    if raw in {"DEGRADED"}:
        return "warn"
    if not raw:
        return "unknown"
    return "warn"
