"""Browse backup destination folders (Copilot local volume + SFTP).

Listing only — no dumping of archive/restic binary contents.
Paths are always resolved under the destination root (no ``../`` escape).
"""

from __future__ import annotations

import posixpath
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.core.docker_control import DockerControlError
from app.core.locale import format_bytes, format_de, iso_utc

from backup_verifier.config import BackupSettings, get_backup_settings
from backup_verifier.destinations import (
    KIND_COPILOT,
    KIND_SFTP,
    public_destination,
    resolve_auth,
)
from backup_verifier import sshutil

ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar")
RESTIC_MARKERS = frozenset({"config", "data", "index", "snapshots", "keys", "locks"})


class BrowserError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def normalize_rel(rel: str | None) -> str:
    """Relative posix path under dest root, or ``""`` for the root itself."""
    raw = (rel or "").replace("\\", "/").strip()
    if "\x00" in raw:
        raise BrowserError("Ungültiger Pfad.")
    raw = raw.lstrip("/")
    parts: list[str] = []
    for part in raw.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise BrowserError("Pfad außerhalb des Ziel-Wurzelverzeichnisses.")
        parts.append(part)
    return "/".join(parts)


def join_under_root(root: str, rel: str | None) -> str:
    """Join ``rel`` onto ``root`` and reject any escape."""
    root_n = posixpath.normpath((root or "/").replace("\\", "/") or "/")
    rel_n = normalize_rel(rel)
    if not rel_n:
        return root_n
    joined = posixpath.normpath(posixpath.join(root_n, rel_n))
    if root_n == "/":
        return joined
    if joined == root_n or joined.startswith(root_n.rstrip("/") + "/"):
        return joined
    raise BrowserError("Pfad außerhalb des Ziel-Wurzelverzeichnisses.")


def entry_kind(name: str, is_dir: bool, *, parent_rel: str) -> str:
    low = (name or "").lower()
    parent = (parent_rel or "").strip("/")
    in_restic = parent == "restic" or parent.startswith("restic/")
    if is_dir:
        if low == "restic" or in_restic:
            return "restic"
        return "dir"
    if any(low.endswith(sfx) for sfx in ARCHIVE_SUFFIXES):
        return "archive"
    if in_restic or low in RESTIC_MARKERS:
        return "restic"
    return "file"


def can_download(kind: str, *, is_dir: bool, blocked: bool = False) -> bool:
    """Only stack ``.tar.gz`` archives — never restic keys/packs or dirs."""
    return (not is_dir) and (not blocked) and kind == "archive"


def kind_label(kind: str, *, is_dir: bool, blocked: bool = False) -> str:
    if blocked:
        return "Verweis (gesperrt)"
    return {
        "dir": "Ordner",
        "archive": "Archiv (tar)",
        "restic": "restic",
        "file": "Datei",
    }.get(kind, "Ordner" if is_dir else "Datei")


def dest_root(dest: dict[str, Any], bsettings: BackupSettings | None = None) -> str:
    bsettings = bsettings or get_backup_settings()
    kind = dest.get("kind")
    raw = (dest.get("remote_path") or "").strip()
    if kind == KIND_COPILOT:
        return raw or str(bsettings.copilot_dir)
    if kind == KIND_SFTP:
        if not raw:
            raise BrowserError("SFTP-Ziel hat keinen Remote-Pfad.", 400)
        return raw
    raise BrowserError(
        "Host-Staging ist ephemer und nicht durchsuchbar.",
        400,
    )


def is_browsable(dest: dict[str, Any] | None) -> bool:
    if not dest:
        return False
    return dest.get("kind") in {KIND_COPILOT, KIND_SFTP}


def crumbs_for(dest: dict[str, Any], rel: str) -> list[dict[str, str]]:
    label = str(dest.get("label") or dest.get("kind") or "Ziel")
    items = [{"name": label, "path": ""}]
    if not rel:
        return items
    acc: list[str] = []
    for part in rel.split("/"):
        acc.append(part)
        items.append({"name": part, "path": "/".join(acc)})
    return items


def _mtime_fields(ts: float | None) -> dict[str, str | None]:
    if ts is None:
        return {"mtime": None, "mtime_iso": None}
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return {"mtime": None, "mtime_iso": None}
    return {"mtime": format_de(dt), "mtime_iso": iso_utc(dt)}


def _entry_dict(
    *,
    name: str,
    rel: str,
    is_dir: bool,
    size: int | None,
    mtime: float | None,
    blocked: bool = False,
    symlink: bool = False,
) -> dict[str, Any]:
    parent = posixpath.dirname(rel)
    kind = entry_kind(name, is_dir, parent_rel=parent)
    download = can_download(kind, is_dir=is_dir, blocked=blocked)
    size_bytes = None if is_dir else (int(size) if size is not None else None)
    return {
        "name": name,
        "path": rel,
        "is_dir": is_dir,
        "kind": kind,
        "kind_label": kind_label(kind, is_dir=is_dir, blocked=blocked),
        "size_bytes": size_bytes,
        "size": format_bytes(size_bytes) if size_bytes is not None else "—",
        "downloadable": download,
        "blocked": blocked,
        "symlink": symlink,
        **_mtime_fields(mtime),
    }


def _local_escapes(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return False
    except (ValueError, OSError):
        return True


def list_local(root: str, rel: str) -> list[dict[str, Any]]:
    root_path = Path(join_under_root(root, "")).resolve()
    target = Path(join_under_root(str(root_path), rel))
    if not target.exists():
        if not rel:
            return []
        raise BrowserError("Ordner nicht gefunden.", 404)
    if target.is_symlink() and _local_escapes(root_path, target):
        raise BrowserError("Pfad außerhalb des Ziel-Wurzelverzeichnisses.")
    if target.exists() and _local_escapes(root_path, target):
        raise BrowserError("Pfad außerhalb des Ziel-Wurzelverzeichnisses.")
    if not target.is_dir():
        raise BrowserError("Kein Verzeichnis.", 400)
    try:
        children = list(target.iterdir())
    except PermissionError as exc:
        raise BrowserError(f"Keine Berechtigung: {exc}", 403) from exc
    except OSError as exc:
        raise BrowserError(f"Ordner nicht lesbar: {exc}", 502) from exc

    entries: list[dict[str, Any]] = []
    for child in children:
        name = child.name
        child_rel = normalize_rel(f"{rel}/{name}" if rel else name)
        symlink = child.is_symlink()
        blocked = False
        is_dir = False
        size: int | None = None
        mtime: float | None = None
        try:
            if symlink and _local_escapes(root_path, child):
                blocked = True
                is_dir = False
            else:
                st = child.lstat() if symlink else child.stat()
                is_dir = child.is_dir() and not blocked
                size = int(st.st_size)
                mtime = float(st.st_mtime)
        except OSError:
            blocked = True
        entries.append(
            _entry_dict(
                name=name,
                rel=child_rel,
                is_dir=is_dir,
                size=size,
                mtime=mtime,
                blocked=blocked,
                symlink=symlink,
            )
        )
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return entries


def resolve_local_file(root: str, rel: str) -> Path:
    if not normalize_rel(rel):
        raise BrowserError("Keine Datei gewählt.", 400)
    root_path = Path(join_under_root(root, "")).resolve()
    target = Path(join_under_root(str(root_path), rel))
    if not target.exists():
        raise BrowserError("Datei nicht gefunden.", 404)
    if _local_escapes(root_path, target):
        raise BrowserError("Pfad außerhalb des Ziel-Wurzelverzeichnisses.")
    if not target.is_file():
        raise BrowserError("Nur Dateien können geladen werden.", 400)
    kind = entry_kind(target.name, False, parent_rel=posixpath.dirname(normalize_rel(rel)))
    if not can_download(kind, is_dir=False):
        raise BrowserError(
            "Download nur für Backup-Archive (.tar.gz). restic-Dateien bleiben unangetastet.",
            400,
        )
    return target


def _sftp_target_escapes(root: str, remote: str, link_target: str) -> bool:
    parent = posixpath.dirname(remote)
    if link_target.startswith("/"):
        resolved = posixpath.normpath(link_target)
    else:
        resolved = posixpath.normpath(posixpath.join(parent, link_target))
    root_n = posixpath.normpath(root.replace("\\", "/") or "/")
    if root_n == "/":
        return False
    return not (resolved == root_n or resolved.startswith(root_n.rstrip("/") + "/"))


async def list_sftp(
    dest: dict[str, Any],
    rel: str,
    *,
    settings: Settings | None = None,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    host = (dest.get("host") or "").strip()
    if not host:
        raise BrowserError("SFTP-Ziel hat keinen Host.", 400)
    root = dest_root(dest)
    remote = join_under_root(root, rel)
    try:
        auth = resolve_auth(dest, settings)
    except DockerControlError as exc:
        raise BrowserError(exc.message, exc.status_code) from exc
    try:
        raw = await sshutil.sftp_listdir(
            settings,
            host,
            remote,
            timeout=timeout,
            username=auth["username"],
            key=auth.get("key"),
            key_pem=auth.get("key_pem"),
            password=auth.get("password"),
            port=auth["port"],
        )
    except DockerControlError as exc:
        raise BrowserError(exc.message, exc.status_code) from exc

    entries: list[dict[str, Any]] = []
    for item in raw:
        name = item["name"]
        child_rel = normalize_rel(f"{rel}/{name}" if rel else name)
        blocked = False
        if item.get("symlink") and item.get("link_target"):
            blocked = _sftp_target_escapes(root, item["path"], str(item["link_target"]))
        is_dir = bool(item.get("is_dir")) and not blocked
        entries.append(
            _entry_dict(
                name=name,
                rel=child_rel,
                is_dir=is_dir,
                size=item.get("size"),
                mtime=item.get("mtime"),
                blocked=blocked,
                symlink=bool(item.get("symlink")),
            )
        )
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return entries


def assert_sftp_downloadable(root: str, rel: str, name: str) -> str:
    remote = join_under_root(root, rel)
    kind = entry_kind(name, False, parent_rel=posixpath.dirname(normalize_rel(rel)))
    if not can_download(kind, is_dir=False):
        raise BrowserError(
            "Download nur für Backup-Archive (.tar.gz). restic-Dateien bleiben unangetastet.",
            400,
        )
    return remote


async def browse_destination(
    dest: dict[str, Any],
    rel: str | None = "",
    *,
    settings: Settings | None = None,
    bsettings: BackupSettings | None = None,
) -> dict[str, Any]:
    if not is_browsable(dest):
        raise BrowserError(
            "Host-Staging ist ephemer und nicht durchsuchbar.",
            400,
        )
    bsettings = bsettings or get_backup_settings()
    settings = settings or get_settings()
    path = normalize_rel(rel)
    root = dest_root(dest, bsettings)
    kind = dest.get("kind")
    message = ""
    try:
        if kind == KIND_COPILOT:
            entries = list_local(root, path)
        else:
            entries = await list_sftp(
                dest,
                path,
                settings=settings,
                timeout=min(60.0, bsettings.backup_ssh_timeout),
            )
    except BrowserError:
        raise
    except OSError as exc:
        raise BrowserError(f"Ziel nicht erreichbar: {exc}", 502) from exc

    if not entries and not path:
        message = "Noch keine Backups — der Ordner ist leer oder fehlt."
    elif not entries:
        message = "Dieser Ordner ist leer."

    parent = posixpath.dirname(path) if path else None
    if parent == ".":
        parent = ""
    return {
        "ok": True,
        "destination": public_destination(dest),
        "root": root,
        "path": path,
        "parent": parent if path else None,
        "crumbs": crumbs_for(dest, path),
        "entries": entries,
        "empty": not entries,
        "message": message,
    }
