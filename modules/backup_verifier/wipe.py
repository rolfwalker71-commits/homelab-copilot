"""Confirmed wipe of backup datastores and job history (never from cron)."""

from __future__ import annotations

import logging
import posixpath
import secrets
import shlex
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.config import Settings, get_settings
from app.core.docker_control import DockerControlError, resolve_parent_ip
from app.core.models import TopologySnapshot

from backup_verifier.config import BackupSettings, get_backup_settings
from backup_verifier.destinations import (
    KIND_SFTP,
    dest_rsync_ssh_port,
    dest_sftp_port,
    is_hetzner_storagebox,
    resolve_auth,
)
from backup_verifier.inventory import resolve_guest
from backup_verifier import sshutil
from backup_verifier.store import BackupStore

logger = logging.getLogger(__name__)

LogFn = Callable[[str], Awaitable[None]]
ProgressFn = Callable[..., Awaitable[None]]

WIPE_KEYWORD_PREFIX = "LÖSCH-"
WIPE_KEYWORD_TTL_S = 15 * 60
WIPE_STACK = "_wipe"

# System / data roots that must never be wiped as a dest or Copilot path.
FORBIDDEN_WIPE_PATHS = frozenset(
    {
        "",
        ".",
        "..",
        "/",
        "/home",
        "/root",
        "/var",
        "/usr",
        "/etc",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/opt",
        "/tmp",
        "/data",
        "/mnt",
        "/media",
        "/boot",
        "/dev",
        "/proc",
        "/sys",
        "/run",
        "/srv",
        "home",
        "root",
        "var",
        "data",
        "tmp",
    }
)

# Never unlink these names even if they appear under the Copilot backup dir.
_PROTECT_FILENAMES = frozenset(
    {
        "backup_verifier.db",
        "app.db",
        "topology.db",
        "inventory.db",
    }
)

_token_lock = threading.Lock()
start_wipe_lock = threading.Lock()
_issued_keyword: str | None = None
_issued_at: float = 0.0


class WipeError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def issue_wipe_keyword() -> str:
    """Create a short visible token the operator must type (dialog only)."""
    global _issued_keyword, _issued_at
    token = WIPE_KEYWORD_PREFIX + secrets.token_hex(3).upper()
    with _token_lock:
        _issued_keyword = token
        _issued_at = time.time()
    return token


def peek_wipe_keyword() -> str | None:
    with _token_lock:
        return _issued_keyword


def consume_wipe_keyword() -> str | None:
    """Return the issued keyword and clear it (one successful POST)."""
    global _issued_keyword, _issued_at
    with _token_lock:
        token = _issued_keyword
        _issued_keyword = None
        _issued_at = 0.0
        return token


def validate_wipe_keyword(
    provided: str | None,
    expected: str | None,
    *,
    issued_at: float | None = None,
    now: float | None = None,
    ttl_s: float = WIPE_KEYWORD_TTL_S,
) -> None:
    """Exact match, including umlauts. Empty / mismatch / expired → WipeError 400."""
    got = "" if provided is None else str(provided)
    want = "" if expected is None else str(expected)
    if not want or got != want:
        raise WipeError(
            "Bestätigungswort stimmt nicht. Bitte das angezeigte Wort genau eintippen.",
            400,
        )
    if issued_at is not None:
        clock = time.time() if now is None else now
        if clock - issued_at > ttl_s:
            raise WipeError(
                "Bestätigung abgelaufen — Dialog neu öffnen und Wort erneut eintippen.",
                400,
            )


def validate_issued_keyword(provided: str | None) -> None:
    """Check against the in-memory token from the last preview (does not consume)."""
    with _token_lock:
        expected = _issued_keyword
        issued_at = _issued_at
    validate_wipe_keyword(provided, expected, issued_at=issued_at)


def normalize_wipe_path(raw: str | None) -> str:
    """Normalize a wipe target; reject NUL, ``..``, empty, and ``.``."""
    p = (raw or "").replace("\\", "/").strip()
    if not p or p in {".", ".."}:
        raise WipeError("Leerer oder ungültiger Löschpfad.", 400)
    if "\x00" in p:
        raise WipeError("Ungültiger Löschpfad.", 400)
    parts = [part for part in p.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise WipeError("Pfad außerhalb des erlaubten Backup-Wurzelverzeichnisses.", 400)
    if p.startswith("/"):
        return posixpath.normpath("/" + "/".join(parts)) if parts else "/"
    return posixpath.normpath("/".join(parts)) if parts else "."


def is_dest_account_root(raw: str | None) -> bool:
    """True when dest ``remote_path`` is Storage-Box / SFTP account root ``/home``."""
    try:
        path = normalize_wipe_path(raw)
    except WipeError:
        return False
    return (path.rstrip("/") or "/") == "/home"


def assert_safe_wipe_path(
    raw: str | None,
    *,
    allowed_root: str | None = None,
    data_dir: str | Path | None = None,
    dest_contents: bool = False,
) -> str:
    """Jail: only a known backup root; never ``/``, DATA_DIR, or Linux ``/home``.

    Dest wipe may pass ``dest_contents=True`` so Storage Box / SFTP ``/home``
    is allowed as a *contents-only* target (never rmdir the account root).
    Guest and Copilot wipes must leave this flag off — guest ``/home`` stays forbidden.
    """
    path = normalize_wipe_path(raw)
    low = path.rstrip("/") or "/"
    allow_home = dest_contents and is_dest_account_root(low)
    if not allow_home and (low in FORBIDDEN_WIPE_PATHS or path in FORBIDDEN_WIPE_PATHS):
        raise WipeError(
            f"Löschpfad „{path}“ ist zu weit (System- oder Datenwurzel) — Abbruch.",
            400,
        )
    if data_dir is not None:
        data_n = normalize_wipe_path(str(data_dir))
        if path == data_n or path == str(Path(data_dir)):
            raise WipeError(
                "Löschpfad ist DATA_DIR — SQLite und SSH-Keys würden verloren gehen.",
                400,
            )
    if allowed_root is not None:
        root = normalize_wipe_path(allowed_root)
        if path != root and not path.startswith(root.rstrip("/") + "/"):
            raise WipeError(
                "Löschpfad liegt nicht unter dem konfigurierten Backup-Ziel.",
                400,
            )
        root_home = dest_contents and is_dest_account_root(root)
        if not root_home and root in FORBIDDEN_WIPE_PATHS:
            raise WipeError(
                f"Konfiguriertes Ziel „{root}“ ist keine sichere Backup-Wurzel.",
                400,
            )
    return path


def assert_copilot_wipe_root(copilot_dir: Path, data_dir: Path) -> Path:
    """Copilot backup dir only — never DATA_DIR itself or ``/``."""
    root = Path(copilot_dir)
    data = Path(data_dir)
    try:
        resolved = root.resolve()
        data_r = data.resolve()
    except OSError as exc:
        raise WipeError(f"Copilot-Pfad nicht auflösbar: {exc}", 400) from exc
    if resolved == data_r:
        raise WipeError(
            "Copilot-Backuppfad ist DATA_DIR — Löschen würde die App-DB treffen.",
            400,
        )
    assert_safe_wipe_path(str(resolved), allowed_root=str(resolved), data_dir=data_r)
    return resolved


def wipe_local_dir_contents(root: Path, *, data_dir: Path | None = None) -> int:
    """Delete children of ``root`` (keep the directory). Skip protected DB names."""
    root_r = assert_copilot_wipe_root(root, data_dir or root.parent)
    if not root_r.is_dir():
        return 0
    removed = 0
    for child in root_r.iterdir():
        if child.name in _PROTECT_FILENAMES:
            continue
        try:
            if child.is_symlink() or child.is_file():
                child.unlink()
                removed += 1
            elif child.is_dir():
                shutil.rmtree(child)
                removed += 1
        except OSError as exc:
            raise WipeError(
                f"Copilot-Pfad nicht löschbar ({child}): {exc}",
                500,
            ) from exc
    return removed


def _dest_label(dest: dict[str, Any]) -> str:
    return str(dest.get("label") or dest.get("kind") or "SFTP")


def public_sftp_wipe_target(dest: dict[str, Any]) -> dict[str, Any]:
    path = str(dest.get("remote_path") or "").strip()
    safe = True
    reason = ""
    try:
        assert_safe_wipe_path(path, allowed_root=path, dest_contents=True)
    except WipeError as exc:
        safe = False
        reason = exc.message
    return {
        "id": dest.get("id"),
        "label": _dest_label(dest),
        "host": dest.get("host") or "",
        "remote_path": path,
        "hetzner": is_hetzner_storagebox(dest=dest),
        "contents_only": safe and is_dest_account_root(path),
        "safe": safe,
        "unsafe_reason": reason,
    }


async def collect_guest_targets(
    store: BackupStore,
    snapshot: TopologySnapshot | None,
) -> list[dict[str, Any]]:
    """Unique LXCs that ever had a backup/schedule/restic repo (or live stacks)."""
    seen: dict[str, dict[str, Any]] = {}

    def add(parent_id: str, project: str = "") -> None:
        pid = str(parent_id or "").strip()
        if not pid:
            return
        if pid not in seen:
            guest = resolve_guest(snapshot, pid)
            seen[pid] = {
                "parent_id": pid,
                "guest_name": guest.get("guest_name") or pid,
                "projects": [],
            }
        proj = str(project or "").strip()
        if proj and proj not in seen[pid]["projects"]:
            seen[pid]["projects"].append(proj)

    for row in await store.list_schedules():
        add(str(row.get("parent_id") or ""), str(row.get("stack") or ""))
    for row in await store.list_restic_stacks():
        add(str(row.get("parent_id") or ""), str(row.get("project") or ""))
    for row in await store.list_history_parents():
        add(str(row.get("parent_id") or ""), str(row.get("stack") or ""))
    if snapshot is not None:
        from backup_verifier.backup import list_backup_stacks

        for row in await list_backup_stacks(snapshot):
            add(str(row.get("parent_id") or ""), str(row.get("stack") or ""))
    return sorted(seen.values(), key=lambda g: str(g.get("guest_name") or ""))


async def build_wipe_preview(
    store: BackupStore,
    *,
    snapshot: TopologySnapshot | None = None,
    bsettings: BackupSettings | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    bsettings = bsettings or get_backup_settings()
    settings = settings or get_settings()
    dests = await store.list_destinations()
    sftp = [d for d in dests if d.get("kind") == KIND_SFTP]
    history = await store.count_job_history()
    guests = await collect_guest_targets(store, snapshot)
    copilot = str(bsettings.copilot_dir)
    copilot_ok = True
    copilot_err = ""
    try:
        assert_copilot_wipe_root(bsettings.copilot_dir, settings.data_dir)
    except WipeError as exc:
        copilot_ok = False
        copilot_err = exc.message
    keyword = issue_wipe_keyword()
    return {
        "confirm_keyword": keyword,
        "irreversible": True,
        "copilot": {
            "path": copilot,
            "safe": copilot_ok,
            "unsafe_reason": copilot_err,
        },
        "destinations": [public_sftp_wipe_target(d) for d in sftp],
        "guests": guests,
        "guest_root": bsettings.backup_lxc_dir,
        "history": history,
        "keeps": [
            "Zeitpläne",
            "Ziel-Zugangsdaten",
            "App-Einstellungen / VAPID",
            "Inventar / Topologie",
            "restic-Passwörter (für denselben Stack)",
        ],
    }


async def _emit(
    on_progress: ProgressFn | None,
    *,
    phase: str,
    percent: int,
    message: str,
    run_id: int | None = None,
) -> None:
    if on_progress:
        await on_progress(phase=phase, percent=percent, message=message, run_id=run_id)


async def _log(on_log: LogFn | None, line: str) -> None:
    if on_log:
        await on_log(line)
    logger.info("backup wipe: %s", line)


async def _log_dest_contents_wiped(
    on_log: LogFn | None,
    dest: dict[str, Any],
    path: str,
    n: int,
) -> None:
    if is_dest_account_root(path):
        prefix = "Hetzner" if is_hetzner_storagebox(dest=dest) else "SFTP"
        await _log(
            on_log,
            f"{prefix}: Inhalt von {path} geleert ({n} Einträge), Wurzel belassen.",
        )
        return
    await _log(on_log, f"SFTP: {n} Einträge unter {path} gelöscht.")


async def _wipe_sftp_dest(
    dest: dict[str, Any],
    *,
    settings: Settings,
    timeout: float,
    on_log: LogFn | None,
) -> None:
    label = _dest_label(dest)
    host = str(dest.get("host") or "").strip()
    raw_path = str(dest.get("remote_path") or "").strip()
    path = assert_safe_wipe_path(raw_path, allowed_root=raw_path, dest_contents=True)
    if not host:
        raise WipeError(f"{label}: kein Host konfiguriert.", 400)
    auth = resolve_auth(dest, settings)
    sftp_port = dest_sftp_port(dest)
    ssh_port = dest_rsync_ssh_port(dest)
    auth_kw = {
        "username": auth["username"],
        "key": auth.get("key"),
        "key_pem": auth.get("key_pem"),
        "password": auth.get("password"),
    }
    await _log(on_log, f"Lösche Dest {label} ({host} → {path})…")

    # Prefer SSH rm on port 23 (Storage Box) — much faster than per-file SFTP.
    ssh_ok = False
    ssh_n = 0
    if not (is_hetzner_storagebox(dest=dest) and ssh_port == 22):
        try:
            entries = await sshutil.sftp_listdir(
                settings,
                host,
                path,
                timeout=min(120.0, timeout),
                port=sftp_port,
                **auth_kw,
            )
            for ent in entries:
                name = str(ent.get("name") or "")
                if name in (".", "..", ""):
                    continue
                child = str(ent.get("path") or "")
                if not child:
                    continue
                child_n = assert_safe_wipe_path(
                    child, allowed_root=path, dest_contents=True
                )
                if child_n == path or is_dest_account_root(child_n):
                    continue
                await sshutil.ssh_run_ok(
                    settings,
                    host,
                    f"rm -rf -- {shlex.quote(posixpath.normpath(child_n))}",
                    timeout=timeout,
                    port=ssh_port,
                    **auth_kw,
                )
                ssh_n += 1
                await _log(on_log, f"  entfernt: {ent.get('name')}")
            ssh_ok = True
        except DockerControlError as exc:
            low = (exc.message or "").lower()
            if "nicht gefunden" in low or exc.status_code == 404:
                await _log_dest_contents_wiped(on_log, dest, path, 0)
                return
            await _log(
                on_log,
                f"SSH-Löschen auf {label} nicht möglich ({exc.message}) — SFTP…",
            )
        except Exception as exc:
            await _log(on_log, f"SSH-Löschen auf {label} fehlgeschlagen ({exc}) — SFTP…")

    if ssh_ok:
        await _log_dest_contents_wiped(on_log, dest, path, ssh_n)
        return

    try:
        n = await sshutil.sftp_rm_tree(
            settings,
            host,
            path,
            contents_only=True,
            timeout=timeout,
            port=sftp_port,
            **auth_kw,
        )
        await _log_dest_contents_wiped(on_log, dest, path, n)
    except DockerControlError as exc:
        low = (exc.message or "").lower()
        if "nicht gefunden" in low or exc.status_code == 404:
            await _log_dest_contents_wiped(on_log, dest, path, 0)
            return
        raise WipeError(f"{label}: {exc.message}", exc.status_code) from exc


async def _wipe_guest(
    *,
    parent_id: str,
    guest_name: str,
    lxc_dir: str,
    snapshot: TopologySnapshot | None,
    settings: Settings,
    timeout: float,
    on_log: LogFn | None,
) -> None:
    path = assert_safe_wipe_path(lxc_dir, allowed_root=lxc_dir)
    try:
        ip = resolve_parent_ip(snapshot, parent_id)
    except DockerControlError as exc:
        raise WipeError(
            f"Gast {guest_name}: {exc.message}",
            exc.status_code,
        ) from exc
    await _log(on_log, f"Lösche Guest-Repos auf {guest_name} ({path})…")
    if ip is None:
        local = Path(path)
        if not local.is_dir():
            await _log(on_log, f"  lokaler Guest-Pfad fehlt bereits: {path}")
            return
        n = 0
        for child in local.iterdir():
            if child.name in _PROTECT_FILENAMES:
                continue
            if child.is_symlink() or child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            n += 1
        await _log(on_log, f"  lokal {n} Einträge entfernt.")
        return
    try:
        quoted = shlex.quote(path)
        await sshutil.ssh_run_ok(
            settings,
            ip,
            f"if [ ! -d {quoted} ]; then exit 0; fi; "
            "rm -rf -- "
            + " ".join(
                shlex.quote(f"{path}/{name}")
                for name in ("restic", "restic-work", "restic-cache", "restore")
            )
            + f" {quoted}/*",
            timeout=timeout,
        )
        await _log(on_log, f"  Guest-Repos auf {guest_name} gelöscht.")
    except DockerControlError as exc:
        low = (exc.message or "").lower()
        if "nicht gefunden" in low or "no such file" in low or exc.status_code == 404:
            await _log(on_log, f"  Guest-Pfad fehlt bereits: {path}")
            return
        raise WipeError(
            f"Gast {guest_name}: {exc.message}",
            exc.status_code,
        ) from exc


async def run_wipe(
    store: BackupStore,
    *,
    confirm_keyword: str,
    wipe_guest: bool = True,
    snapshot: TopologySnapshot | None = None,
    settings: Settings | None = None,
    bsettings: BackupSettings | None = None,
    on_progress: ProgressFn | None = None,
    on_log: LogFn | None = None,
    run_id: int | None = None,
    prevalidated: bool = False,
) -> dict[str, Any]:
    """Execute a confirmed wipe. Caller must already refuse in-memory running jobs."""
    if not prevalidated:
        validate_issued_keyword(confirm_keyword)
        consume_wipe_keyword()

    settings = settings or get_settings()
    bsettings = bsettings or get_backup_settings()
    timeout = max(600.0, float(bsettings.backup_transfer_timeout))
    errors: list[str] = []
    warnings: list[str] = []

    await _emit(
        on_progress,
        phase="Copilot",
        percent=8,
        message="Lösche Copilot-Datastore…",
        run_id=run_id,
    )
    copilot_n = 0
    try:
        root = assert_copilot_wipe_root(bsettings.copilot_dir, settings.data_dir)
        await _log(on_log, f"Lösche Copilot-Datastore: {root}")
        copilot_n = wipe_local_dir_contents(root, data_dir=settings.data_dir)
        await _log(on_log, f"Copilot: {copilot_n} Einträge entfernt.")
    except WipeError as exc:
        errors.append(exc.message)
        await _log(on_log, f"Fehler Copilot: {exc.message}")

    dests = [d for d in await store.list_destinations() if d.get("kind") == KIND_SFTP]
    n_dest = max(len(dests), 1)
    wiped_dests: list[str] = []
    for i, dest in enumerate(dests):
        label = _dest_label(dest)
        pct = 25 + int(40 * (i / n_dest))
        await _emit(
            on_progress,
            phase=f"Dest · {label}",
            percent=pct,
            message=f"Lösche {label}…",
            run_id=run_id,
        )
        try:
            await _wipe_sftp_dest(
                dest, settings=settings, timeout=timeout, on_log=on_log
            )
            wiped_dests.append(label)
        except WipeError as exc:
            errors.append(exc.message)
            await _log(on_log, f"Fehler {label}: {exc.message}")
        except DockerControlError as exc:
            errors.append(f"{label}: {exc.message}")
            await _log(on_log, f"Fehler {label}: {exc.message}")

    guest_wiped: list[str] = []
    if wipe_guest:
        guests = await collect_guest_targets(store, snapshot)
        n_g = max(len(guests), 1)
        if not guests:
            await _log(on_log, "Keine bekannten Guest-Repos (Topologie/Verlauf leer).")
        for i, guest in enumerate(guests):
            name = str(guest.get("guest_name") or guest.get("parent_id"))
            await _emit(
                on_progress,
                phase=f"Guest · {name}",
                percent=70 + int(15 * (i / n_g)),
                message=f"Lösche Guest-Repo auf {name}…",
                run_id=run_id,
            )
            try:
                await _wipe_guest(
                    parent_id=str(guest["parent_id"]),
                    guest_name=name,
                    lxc_dir=bsettings.backup_lxc_dir,
                    snapshot=snapshot,
                    settings=settings,
                    timeout=min(timeout, 900.0),
                    on_log=on_log,
                )
                guest_wiped.append(name)
            except WipeError as exc:
                warnings.append(exc.message)
                await _log(on_log, f"Hinweis Guest {name}: {exc.message}")
    else:
        await _log(
            on_log,
            "Guest-Repos übersprungen — nächster Hop spiegelt alte restic-Historie erneut.",
        )

    if errors:
        await _emit(
            on_progress,
            phase="Fehler",
            percent=95,
            message="Zurücksetzen unvollständig — Verlauf bleibt erhalten.",
            run_id=run_id,
        )
        raise WipeError(
            "Zurücksetzen fehlgeschlagen: " + " · ".join(errors),
            500,
        )

    await _emit(
        on_progress,
        phase="Verlauf",
        percent=92,
        message="Lösche Verlauf…",
        run_id=run_id,
    )
    await store.reset_restic_full_markers()
    history = await store.wipe_job_history()
    await _log(on_log, "Verlauf geleert (Läufe, Restores, Drills).")

    status = "partial" if warnings else "success"
    result = {
        "status": status,
        "copilot_removed": copilot_n,
        "destinations": wiped_dests,
        "guests": guest_wiped,
        "wipe_guest": wipe_guest,
        "history_cleared": history,
        "warnings": warnings,
        "message": (
            "Datastores und Verlauf gelöscht."
            if not warnings
            else "Datastores gelöscht; Guest teilweise: " + " · ".join(warnings)
        ),
    }
    await _emit(
        on_progress,
        phase="Fertig",
        percent=100,
        message=result["message"],
        run_id=None,
    )
    return result
