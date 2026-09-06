"""Soll / Ist for today's backup, patch, and image work (no LLM)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.locale import BERLIN, now_berlin
from backup_verifier.scheduler import cron_clock_hm, schedule_clock_hm
from ops_agent.backup_coverage import (
    host_needs_backup_nag,
    schedule_covers_host,
)
from ops_agent.planner import (
    KIND_BACKUP,
    KIND_PATCH,
    STATUS_ACCEPTED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_WAITING,
)

STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_SKIP = "skipped"
STATUS_QUEUED = "queued"
STATUS_RUN = "running"
STATUS_NO_PLAN = "no_plan"
STATUS_EMPTY = "empty"

STATUS_LABELS_DE = {
    STATUS_OK: "ok",
    STATUS_FAIL: "Fehler",
    STATUS_SKIP: "übersprungen",
    STATUS_QUEUED: "in der Warteschlange",
    STATUS_RUN: "läuft",
    STATUS_NO_PLAN: "kein Plan",
    STATUS_EMPTY: "nichts offen",
}


def _day_iso(dt: datetime | None, *, now: datetime | None = None) -> str:
    if dt is None:
        local = now or now_berlin()
    else:
        local = dt
    if local.tzinfo is None:
        local = local.replace(tzinfo=BERLIN)
    else:
        local = local.astimezone(BERLIN)
    return local.date().isoformat()


def _parse_iso(raw: Any) -> datetime | None:
    iso = str(raw or "").strip()
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BERLIN)
    return dt.astimezone(BERLIN)


def _hm_of(dt: datetime | None, fallback: str = "") -> str:
    if dt is None:
        return fallback
    return dt.strftime("%H:%M")


def _window_day(row: dict[str, Any], *, now: datetime) -> str | None:
    for key in ("updated_at_iso", "start_iso", "created_at_iso"):
        dt = _parse_iso(row.get(key))
        if dt is not None:
            return _day_iso(dt, now=now)
    return None


def _is_today(row: dict[str, Any], *, now: datetime) -> bool:
    day = _window_day(row, now=now)
    return day == _day_iso(now)


def _status_from_window(row: dict[str, Any] | None) -> str:
    if not row:
        return STATUS_EMPTY
    st = str(row.get("status") or "")
    if st == STATUS_DONE:
        return STATUS_OK
    if st == STATUS_FAILED:
        return STATUS_FAIL
    if st == STATUS_SKIPPED:
        return STATUS_SKIP
    if st == STATUS_RUNNING:
        return STATUS_RUN
    if st in (STATUS_ACCEPTED, STATUS_WAITING):
        return STATUS_QUEUED
    return STATUS_EMPTY


def _status_from_run(run: dict[str, Any] | None) -> str:
    if not run:
        return STATUS_EMPTY
    st = str(run.get("status") or "").lower()
    if st in ("success", "ok", "done"):
        return STATUS_OK
    if st in ("partial",):
        return STATUS_OK
    if st in ("failed", "error", "fail"):
        return STATUS_FAIL
    if st in ("running", "queued"):
        return STATUS_RUN
    return STATUS_EMPTY


def _row(
    *,
    kind: str,
    target_id: str,
    target_name: str,
    stack: str = "",
    soll_hm: str = "",
    ist_hm: str = "",
    status: str,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "kind_label": {
            "backup": "Backup",
            "patch": "Patch",
            "images": "Images",
        }.get(kind, kind),
        "target_id": target_id,
        "target_name": target_name,
        "stack": stack,
        "label": f"{target_name} · {stack}" if stack else target_name,
        "soll_hm": soll_hm,
        "ist_hm": ist_hm,
        "status": status,
        "status_label": STATUS_LABELS_DE.get(status, status),
        "reason": reason,
    }


def build_day_ledger(
    *,
    now: datetime | None = None,
    schedules: list[dict[str, Any]] | None = None,
    windows: list[dict[str, Any]] | None = None,
    runs: list[dict[str, Any]] | None = None,
    hosts: list[dict[str, Any]] | None = None,
    patch_scope_ids: list[str] | None = None,
    image_scope_ids: list[str] | None = None,
    prompts: list[dict[str, Any]] | None = None,
    pending: list[Any] | None = None,
    skip_backup_ids: set[str] | None = None,
    backup_stacks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Today's planned vs actual: backups, patches, images."""
    now = now or now_berlin()
    today = _day_iso(now)
    schedules = list(schedules or [])
    windows = list(windows or [])
    runs = list(runs or [])
    hosts = list(hosts or [])
    prompts = list(prompts or [])
    pending = list(pending or [])
    skip_backup = {str(x).strip() for x in (skip_backup_ids or set()) if str(x).strip()}
    patch_ids = {str(x).strip().lower() for x in (patch_scope_ids or []) if str(x).strip()}
    image_ids = {str(x).strip().lower() for x in (image_scope_ids or []) if str(x).strip()}

    pending_by_id: dict[str, Any] = {}
    for host in pending:
        tid = str(getattr(host, "target_id", None) or getattr(host, "id", "") or "")
        if tid:
            pending_by_id[tid.lower()] = host

    backups = _backup_rows(
        now=now,
        today=today,
        schedules=schedules,
        windows=windows,
        runs=runs,
        hosts=hosts,
        prompts=prompts,
        skip_backup=skip_backup,
        backup_stacks=list(backup_stacks or []),
    )
    patches, images = _patch_image_rows(
        now=now,
        today=today,
        windows=windows,
        hosts=hosts,
        patch_ids=patch_ids,
        image_ids=image_ids,
        pending_by_id=pending_by_id,
    )
    return {
        "day": today,
        "backups": backups,
        "patches": patches,
        "images": images,
    }


def _backup_rows(
    *,
    now: datetime,
    today: str,
    schedules: list[dict[str, Any]],
    windows: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    hosts: list[dict[str, Any]],
    prompts: list[dict[str, Any]],
    skip_backup: set[str],
    backup_stacks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    covered: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []

    today_windows = [
        w
        for w in windows
        if str(w.get("kind") or "") == KIND_BACKUP and _is_today(w, now=now)
    ]
    today_runs = [
        r
        for r in runs
        if _day_iso(_parse_iso(r.get("created_at_iso") or r.get("finished_at_iso")), now=now)
        == today
        or str(r.get("status") or "") in ("running", "queued")
    ]

    for sched in schedules:
        parent = str(sched.get("parent_id") or "").strip()
        stack = str(sched.get("stack") or "").strip()
        if not parent or not stack:
            continue
        covered.add((parent.lower(), stack.lower()))
        name = str(sched.get("guest_name") or parent)
        hm = schedule_clock_hm(sched)
        if hm is None:
            clock = cron_clock_hm(str(sched.get("cron_expr") or ""))
            hm = clock
        soll = f"{hm[0]:02d}:{hm[1]:02d}" if hm else str(sched.get("start_hm") or "")
        enabled = bool(sched.get("enabled", True))
        win = _best_window(
            today_windows,
            target_id=parent,
            stack=stack,
            schedule_id=sched.get("id"),
        )
        run = _best_run(today_runs, parent_id=parent, stack=stack)
        status = _status_from_window(win)
        if status == STATUS_EMPTY:
            status = _status_from_run(run)
        if status == STATUS_EMPTY:
            status = STATUS_QUEUED if enabled else STATUS_SKIP
        ist_dt = _parse_iso((win or {}).get("updated_at_iso")) or _parse_iso(
            (run or {}).get("finished_at_iso") or (run or {}).get("created_at_iso")
        )
        ist = _hm_of(ist_dt, str((win or {}).get("start_hm") or ""))
        reason = ""
        if not enabled:
            reason = "Zeitplan ist aus."
        elif status == STATUS_QUEUED:
            reason = f"Soll {soll or '—'} — noch nicht gelaufen."
        elif win and str(win.get("reason") or ""):
            reason = str(win.get("reason") or "")
        out.append(
            _row(
                kind="backup",
                target_id=parent,
                target_name=name,
                stack=stack,
                soll_hm=soll,
                ist_hm=ist,
                status=status,
                reason=reason,
            )
        )

    for prompt in prompts:
        if str(prompt.get("kind") or "") != "no_backup":
            continue
        tid = str(prompt.get("target_id") or "").strip()
        if not tid:
            continue
        host = {
            "id": tid,
            "name": str(prompt.get("target_name") or tid),
            "kind": "lxc",
        }
        if any(schedule_covers_host(s, host) for s in schedules):
            continue
        if any(c[0] == tid.lower() for c in covered):
            continue
        if backup_stacks and not host_needs_backup_nag(host, backup_stacks, schedules):
            continue
        if not backup_stacks:
            # Ohne Stack-Liste: kein Nag nur wegen eines LXC-Namens.
            continue
        name = str(prompt.get("target_name") or tid)
        out.append(
            _row(
                kind="backup",
                target_id=tid,
                target_name=name,
                status=STATUS_NO_PLAN,
                reason=str(prompt.get("reason") or f"{name} hat keinen Backup-Plan."),
            )
        )
        covered.add((tid.lower(), ""))

    for host in hosts:
        tid = str(host.get("id") or host.get("target_id") or "").strip()
        if not tid or tid.lower() in skip_backup:
            continue
        if any(c[0] == tid.lower() for c in covered):
            continue
        if any(schedule_covers_host(s, host) for s in schedules):
            continue
        kind = str(host.get("kind") or "")
        if kind not in ("lxc", "qemu", "lxc-ct", "vm", "manual", "host"):
            continue
        if host.get("gone"):
            continue
        if not host_needs_backup_nag(host, backup_stacks, schedules):
            continue
        name = str(host.get("name") or host.get("target_name") or tid)
        missing = [
            str(s.get("stack") or s.get("compose_project") or "")
            for s in backup_stacks
            if str(s.get("parent_id") or "") == tid
            or str(s.get("guest_name") or "").strip().lower()
            == str(name).strip().lower()
        ]
        stack_hint = next((m for m in missing if m), "")
        reason = f"{name} hat keinen Backup-Plan. So gewollt?"
        if stack_hint:
            reason = f"{name} hat keinen Backup-Plan für {stack_hint}. So gewollt?"
        out.append(
            _row(
                kind="backup",
                target_id=tid,
                target_name=name,
                stack=stack_hint,
                status=STATUS_NO_PLAN,
                reason=reason,
            )
        )
    return out


def _patch_image_rows(
    *,
    now: datetime,
    today: str,
    windows: list[dict[str, Any]],
    hosts: list[dict[str, Any]],
    patch_ids: set[str],
    image_ids: set[str],
    pending_by_id: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _ = today
    patch_out: list[dict[str, Any]] = []
    image_out: list[dict[str, Any]] = []
    seen_patch: set[str] = set()
    seen_image: set[str] = set()

    def _add(kind: str, tid: str, name: str, win: dict[str, Any] | None, reason: str) -> None:
        bucket_wins = image_out if kind == "images" else patch_out
        seen = seen_image if kind == "images" else seen_patch
        key = tid.lower()
        if key in seen:
            return
        seen.add(key)
        status = _status_from_window(win) if win else STATUS_EMPTY
        ist = ""
        soll = ""
        if win:
            soll = str(win.get("start_hm") or "")
            ist_dt = _parse_iso(win.get("updated_at_iso"))
            ist = _hm_of(ist_dt, soll if status in (STATUS_QUEUED, STATUS_RUN) else "")
            if not reason:
                reason = str(win.get("reason") or "")
        bucket_wins.append(
            _row(
                kind=kind,
                target_id=tid,
                target_name=name,
                soll_hm=soll,
                ist_hm=ist,
                status=status,
                reason=reason,
            )
        )

    host_by_id = {
        str(h.get("id") or h.get("target_id") or "").strip().lower(): h
        for h in hosts
        if str(h.get("id") or h.get("target_id") or "").strip()
    }

    ids: list[str] = []
    for raw in list(patch_ids) + list(image_ids) + list(pending_by_id) + [
        str(w.get("target_id") or "").strip().lower()
        for w in windows
        if str(w.get("kind") or "") == KIND_PATCH
    ]:
        if raw and raw not in ids:
            ids.append(raw)

    for key in ids:
        host = host_by_id.get(key)
        pending = pending_by_id.get(key)
        tid = ""
        name = key
        if host:
            tid = str(host.get("id") or host.get("target_id") or key)
            name = str(host.get("name") or host.get("target_name") or tid)
        elif pending is not None:
            tid = str(getattr(pending, "target_id", key) or key)
            name = str(getattr(pending, "target_name", tid) or tid)
        else:
            tid = key
        in_patch = key in patch_ids
        in_image = key in image_ids
        pkgs = list(getattr(pending, "packages", None) or []) if pending is not None else []
        img_n = int(getattr(pending, "image_updates", 0) or 0) if pending is not None else 0
        img_names = list(getattr(pending, "image_names", None) or []) if pending is not None else []
        has_pkg = bool(pkgs)
        has_img = img_n > 0 or bool(img_names)

        patch_win = _best_window(
            [
                w
                for w in windows
                if str(w.get("kind") or "") == KIND_PATCH
                and str(w.get("bucket") or "") != "images"
                and str(w.get("target_id") or "").strip().lower() == key
                and (
                    _is_today(w, now=now)
                    or str(w.get("status") or "")
                    in (STATUS_ACCEPTED, STATUS_WAITING, STATUS_RUNNING)
                )
            ],
            target_id=tid,
        )
        image_win = _best_window(
            [
                w
                for w in windows
                if str(w.get("kind") or "") == KIND_PATCH
                and str(w.get("bucket") or "") == "images"
                and str(w.get("target_id") or "").strip().lower() == key
                and (
                    _is_today(w, now=now)
                    or str(w.get("status") or "")
                    in (STATUS_ACCEPTED, STATUS_WAITING, STATUS_RUNNING)
                )
            ],
            target_id=tid,
        )

        if in_patch or patch_win or (has_pkg and not in_patch):
            if patch_win:
                _add("patch", tid, name, patch_win, "")
            elif not in_patch:
                _add(
                    "patch",
                    tid,
                    name,
                    None,
                    "Host nicht in Matrix (Patchen).",
                )
            elif has_pkg:
                _add(
                    "patch",
                    tid,
                    name,
                    None,
                    "Scan hat offene Pakete — noch kein Fenster. „Plan vorschlagen“ oder nach Scan warten.",
                )
            else:
                _add(
                    "patch",
                    tid,
                    name,
                    None,
                    "Scan hat nichts" if pending is not None else "nichts offen — kein Scan-Fund.",
                )

        if in_image or image_win or (has_img and not in_image):
            if image_win:
                _add("images", tid, name, image_win, "")
            elif not in_image:
                _add(
                    "images",
                    tid,
                    name,
                    None,
                    "Host nicht in Matrix (Images).",
                )
            elif has_img:
                _add(
                    "images",
                    tid,
                    name,
                    None,
                    "Scan hat Image-Updates — noch kein Fenster. „Plan vorschlagen“ oder nach Scan warten.",
                )
            else:
                _add(
                    "images",
                    tid,
                    name,
                    None,
                    "Scan hat nichts" if pending is not None else "nichts offen — kein Image-Fund.",
                )

    return patch_out, image_out


def _best_window(
    rows: list[dict[str, Any]],
    *,
    target_id: str,
    stack: str | None = None,
    schedule_id: Any = None,
) -> dict[str, Any] | None:
    want = str(target_id or "").strip().lower()
    stack_l = str(stack or "").strip().lower()
    sid = None
    try:
        sid = int(schedule_id) if schedule_id is not None else None
    except (TypeError, ValueError):
        sid = None
    ranked: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("target_id") or "").strip().lower() != want:
            continue
        if stack is not None and str(row.get("stack") or "").strip().lower() != stack_l:
            continue
        if sid is not None:
            try:
                if int(row.get("schedule_id") or 0) == sid:
                    ranked.append(row)
                    continue
            except (TypeError, ValueError):
                pass
        ranked.append(row)
    if not ranked:
        return None
    order = {
        STATUS_RUNNING: 0,
        STATUS_ACCEPTED: 1,
        STATUS_WAITING: 2,
        STATUS_FAILED: 3,
        STATUS_DONE: 4,
        STATUS_SKIPPED: 5,
    }
    ranked.sort(
        key=lambda r: (
            order.get(str(r.get("status") or ""), 9),
            str(r.get("updated_at_iso") or r.get("start_iso") or ""),
        )
    )
    return ranked[0]


def _best_run(
    runs: list[dict[str, Any]], *, parent_id: str, stack: str
) -> dict[str, Any] | None:
    want_p = parent_id.strip().lower()
    want_s = stack.strip().lower()
    for row in runs:
        pid = str(row.get("parent_id") or "").strip().lower()
        st = str(row.get("stack") or row.get("project") or "").strip().lower()
        if pid == want_p and st == want_s:
            return row
    return None
