"""Restore destination rules: path jail, staging vs original, typed confirm."""

from __future__ import annotations

import posixpath
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.locale import BERLIN

from backup_verifier.browser import BrowserError, normalize_rel

DEST_STAGING = "staging"
DEST_ORIGINAL = "original"
PLACE_COPILOT = "copilot"
PLACE_GUEST = "guest"
SCOPE_STACK = "stack"
SCOPE_PATHS = "paths"
TYPED_RESTORE = "RESTORE"

_SAFE_MEMBER = re.compile(r"^[\w./@+=:,-]+$")


class RestorePlanError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def normalize_dest_mode(value: str | None) -> str:
    mode = str(value or DEST_STAGING).strip().lower()
    if mode in ("stage", "staging", "nach_staging"):
        return DEST_STAGING
    if mode in ("original", "live", "originalpfad", "an_originalpfad"):
        return DEST_ORIGINAL
    raise RestorePlanError(
        "Zielmodus muss „staging“ (nach Staging) oder „original“ (an Originalpfad) sein."
    )


def normalize_dest_place(value: str | None, *, dest_mode: str) -> str:
    place = str(value or PLACE_COPILOT).strip().lower()
    if dest_mode == DEST_ORIGINAL:
        return PLACE_GUEST
    if place in (PLACE_COPILOT, "copilot_staging", "local"):
        return PLACE_COPILOT
    if place in (PLACE_GUEST, "lxc", "host", "guest_staging"):
        return PLACE_GUEST
    raise RestorePlanError(
        "Staging-Ort muss „copilot“ oder „guest“ (LXC) sein."
    )


def normalize_scope(value: str | None, paths: list[str] | None) -> str:
    scope = str(value or "").strip().lower()
    if scope in (SCOPE_STACK, "ganzer_stack", "all", "binds"):
        return SCOPE_STACK
    if scope in (SCOPE_PATHS, "files", "dateien"):
        return SCOPE_PATHS
    if paths:
        return SCOPE_PATHS
    return SCOPE_STACK


def jail_member_path(rel: str | None) -> str:
    """Relative archive/restic member — no ``..``, no absolute, no NUL."""
    raw = (rel or "").replace("\\", "/").strip()
    if not raw or raw in {".", "./"}:
        raise RestorePlanError("Leerer Wiederherstellungspfad.")
    if "\x00" in raw:
        raise RestorePlanError("Ungültiger Pfad.")
    if raw.startswith("/"):
        raw = raw.lstrip("/")
    try:
        jailed = normalize_rel(raw)
    except BrowserError as exc:
        raise RestorePlanError(exc.message) from exc
    if not jailed:
        raise RestorePlanError("Pfad außerhalb des Archivs.")
    if jailed.startswith("../") or "/../" in f"/{jailed}/":
        raise RestorePlanError("Pfad außerhalb des Archivs.")
    return jailed


def jail_restore_paths(paths: list[str] | None, *, scope: str) -> list[str]:
    if scope == SCOPE_STACK:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in paths or []:
        jailed = jail_member_path(str(raw))
        if jailed in seen:
            continue
        seen.add(jailed)
        out.append(jailed)
    if not out:
        raise RestorePlanError(
            "Keine Dateien gewählt — Pfade angeben oder „ganzer Stack“ wählen."
        )
    return out


def validate_typed_confirm(
    typed: str | None,
    *,
    dest_mode: str,
    stack: str,
) -> None:
    if dest_mode != DEST_ORIGINAL:
        return
    expect = (stack or "").strip()
    got = (typed or "").strip()
    if not got:
        raise RestorePlanError(
            "Originalpfad erfordert eine zweite Bestätigung: "
            "Stack-Name oder „RESTORE“ eintippen."
        )
    if got.upper() == TYPED_RESTORE:
        return
    if expect and got == expect:
        return
    raise RestorePlanError(
        "Zweite Bestätigung ungültig. Tippe den Stack-Namen oder „RESTORE“."
    )


def validate_restore_confirm(
    *,
    confirm: bool,
    dest_mode: str,
    typed_confirm: str | None,
    stack: str,
) -> None:
    if not confirm:
        raise RestorePlanError(
            "Wiederherstellung erfordert Bestätigung (confirm=true)."
        )
    validate_typed_confirm(typed_confirm, dest_mode=dest_mode, stack=stack)


def staging_stamp(now: datetime | None = None) -> str:
    dt = now or datetime.now(BERLIN)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BERLIN)
    else:
        dt = dt.astimezone(BERLIN)
    return dt.strftime("%Y%m%d-%H%M%S")


def guest_staging_dir(lxc_dir: str, project: str, stamp: str | None = None) -> str:
    stamp = stamp or staging_stamp()
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", project or "stack").strip("_") or "stack"
    return f"{lxc_dir.rstrip('/')}/restore/{safe}/{stamp}"


def copilot_staging_dir(copilot_dir: Path, project: str, stamp: str | None = None) -> Path:
    stamp = stamp or staging_stamp()
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", project or "stack").strip("_") or "stack"
    return Path(copilot_dir) / "_restore" / safe / stamp


def describe_restore(
    *,
    stack: str,
    source_label: str,
    snapshot_or_archive: str,
    dest_mode: str,
    dest_place: str,
    scope: str,
    paths: list[str],
    staging_path: str,
) -> dict[str, Any]:
    mode = normalize_dest_mode(dest_mode)
    place = normalize_dest_place(dest_place, dest_mode=mode)
    if mode == DEST_STAGING:
        dest_label = (
            "Copilot-Staging (kein Überschreiben der Live-Binds)"
            if place == PLACE_COPILOT
            else "Restore-Verzeichnis auf dem Gast (kein Live-Overwrite)"
        )
        irreversible = (
            "Staging-Restore ist umkehrbar: Live-Daten bleiben unangetastet. "
            "Vorhandene Dateien im Staging-Ordner werden überschrieben."
        )
    else:
        dest_label = "Originalpfade auf dem Gast — Live-Binds/Volumes werden überschrieben"
        irreversible = (
            "Achtung: Original-Restore überschreibt Live-Daten und ist praktisch "
            "nicht umkehrbar. Stack wird gestoppt."
        )
    if scope == SCOPE_STACK:
        what = "Ganzer Stack / enthaltene Binds"
    else:
        what = f"{len(paths)} ausgewählte Pfad(e)"
    return {
        "stack": stack,
        "source_label": source_label,
        "snapshot_or_archive": snapshot_or_archive,
        "dest_mode": mode,
        "dest_place": place,
        "dest_label": dest_label,
        "scope": scope,
        "scope_label": what,
        "paths": paths,
        "staging_path": staging_path,
        "overwrite": mode == DEST_ORIGINAL,
        "requires_typed_confirm": mode == DEST_ORIGINAL,
        "warning": irreversible,
        "summary": (
            f"{source_label} → {dest_label}. {what}. {irreversible}"
        ),
    }


def infer_stack_from_browse_path(rel: str) -> dict[str, str]:
    """Best-effort stack/project from a browsed dest-relative path."""
    try:
        path = normalize_rel(rel)
    except BrowserError:
        path = (rel or "").replace("\\", "/").strip().lstrip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return {"project": "", "parent_id": "", "kind": ""}
    if parts[0] == "restic":
        parent = parts[1] if len(parts) > 1 else ""
        project = parts[2] if len(parts) > 2 else ""
        return {"project": project, "parent_id": parent, "kind": "restic"}
    name = parts[-1]
    low = name.lower()
    if low.endswith(".tar.gz") or low.endswith(".tgz") or low.endswith(".tar"):
        project = parts[0] if len(parts) > 1 else posixpath.splitext(posixpath.splitext(name)[0])[0]
        if project.endswith(".tar"):
            project = project[: -len(".tar")]
        return {"project": project, "parent_id": "", "kind": "tar"}
    return {"project": parts[0], "parent_id": "", "kind": "dir"}
