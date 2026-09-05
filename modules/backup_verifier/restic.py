"""Restic incremental backups: install, init, backup, prune, sync, restore."""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.config import Settings, get_settings
from app.core.docker_control import DockerControlError
from app.core.locale import BERLIN, format_de, iso_utc, now_berlin

from backup_verifier.config import BackupSettings, get_backup_settings
from backup_verifier.destinations import (
    KIND_COPILOT,
    KIND_HOST,
    KIND_SFTP,
    legacy_role_for,
    resolve_auth,
)
from backup_verifier import sshutil
from backup_verifier.store import BackupStore

logger = logging.getLogger(__name__)

LogFn = Callable[[str], Awaitable[None]]
ProgressFn = Callable[..., Awaitable[None]]

ENGINE_TAR = "tar"
ENGINE_RESTIC = "restic"

_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")


class ResticError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def safe_name(value: str) -> str:
    return _SAFE.sub("_", value or "stack").strip("_") or "stack"


def lxc_repo_path(lxc_dir: str, project: str) -> str:
    return f"{lxc_dir.rstrip('/')}/restic/{safe_name(project)}"


def copilot_repo_path(copilot_dir: Path, parent_id: str, project: str) -> Path:
    return copilot_dir / "restic" / safe_name(parent_id) / safe_name(project)


def sftp_repo_rel(parent_id: str, project: str) -> str:
    return f"restic/{safe_name(parent_id)}/{safe_name(project)}"


_APT_LOCK_HINT = re.compile(
    r"could not get lock|unable to lock directory|unable to acquire the dpkg|"
    r"dpkg frontend lock|apt_lock=1",
    re.I,
)


def translate_restic_error(text: str) -> str:
    """Map restic/SSH failures to clear German messages (never include secrets)."""
    raw = (text or "").strip()
    low = raw.lower()
    if "wrong password" in low or "ciphertext verification" in low:
        return (
            "Falsches restic-Passwort. Das in Copilot gespeicherte Repo-Passwort "
            "passt nicht zum Repository."
        )
    if _APT_LOCK_HINT.search(raw):
        return (
            "APT-Sperre auf dem Host (unattended-upgrades oder apt). "
            "Später erneut versuchen — restic wird nicht parallel zu apt installiert."
        )
    if (
        "unable to create lock" in low
        or "repository is already locked" in low
        or ("lock" in low and "timeout" in low and "apt" not in low)
    ):
        return (
            "restic-Repository ist gesperrt (laufendes Backup oder abgestürzter "
            "Prozess). Copilot versucht unlock — bitte später erneut versuchen."
        )
    if "ssh-timeout" in low or (
        "timeout" in low and "host nicht erreichbar" in low
    ):
        return (
            "SSH-Timeout — der Befehl dauerte zu lange (Host oft erreichbar). "
            "restic-Installation läuft getrennt mit RESTIC_INSTALL_TIMEOUT "
            "(Default 600s), nicht mit BACKUP_SSH_TIMEOUT."
        )
    if "restic-installation timeout" in low or (
        "timeout" in low and "restic" in low and "install" in low
    ):
        return (
            "restic-Installation Timeout (apt/apk zu langsam). "
            "RESTIC_INSTALL_TIMEOUT erhöhen oder restic manuell auf dem LXC installieren."
        )
    if "restic: not found" in low or (
        "command not found" in low and "restic" in low
    ):
        return (
            "restic fehlt auf dem Host. Copilot kopiert das Binary aus dem "
            "Copilot-Image oder installiert per apt/apk, wenn RESTIC_INSTALL=true "
            "(Default). Sonst restic auf dem LXC installieren."
        )
    if "kein apt/apk" in low:
        return (
            "Kein apt/apk auf dem Host — restic kann nicht per Paketmanager "
            "installiert werden. Binary-Kopie aus dem Copilot-Image fehlgeschlagen "
            "oder RESTIC_INSTALL=false."
        )
    if "config file" in low and ("not found" in low or "does not exist" in low):
        return "restic-Repository ist nicht initialisiert oder der Pfad ist falsch."
    if "no snapshot" in low or "snapshot does not exist" in low:
        return "restic-Snapshot nicht gefunden. Liste unter Verlauf aktualisieren."
    if "permission denied" in low:
        return "Keine Berechtigung für restic-Repository oder Quellpfade."
    # Strip anything that looks like a password assignment from leftover output
    cleaned = re.sub(r"(RESTIC_PASSWORD|password)=[^\s]+", r"\1=***", raw, flags=re.I)
    if len(cleaned) > 400:
        cleaned = cleaned[:399].rstrip() + "…"
    return cleaned or "restic-Befehl fehlgeschlagen."


def _should_run_full(last_full_iso: str | None, every_days: int) -> bool:
    if not last_full_iso or every_days <= 0:
        return True
    try:
        raw = last_full_iso.replace("Z", "+00:00")
        last = datetime.fromisoformat(raw)
        if last.tzinfo is None:
            last = last.replace(tzinfo=BERLIN)
        return now_berlin() - last.astimezone(BERLIN) >= timedelta(days=every_days)
    except (TypeError, ValueError):
        return True


def collect_restic_paths(inventory: dict[str, Any]) -> list[dict[str, str]]:
    """Inventory paths restic will back up (same scope as tar)."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(kind: str, path: str, name: str = "") -> None:
        path = (path or "").strip()
        if not path or path in seen:
            return
        seen.add(path)
        out.append({"kind": kind, "path": path, "name": name or path})

    for cf in inventory.get("compose_files") or []:
        add("compose", cf)
    env = inventory.get("env_file")
    if env:
        add("env", env)
    for vol in inventory.get("named_volumes") or []:
        src = (vol.get("source") or "").strip()
        add("volume", src or vol.get("name") or "", vol.get("name") or "")
    for bind in inventory.get("bind_mounts") or []:
        if not bind.get("readable") and bind.get("will_backup") is False:
            continue
        if bind.get("will_backup") or bind.get("readable"):
            add("bind", bind.get("source") or "")
    return out


async def _emit_progress(
    on_progress: ProgressFn | None,
    *,
    phase: str,
    percent: int,
    message: str,
    run_id: int | None = None,
) -> None:
    if not on_progress:
        return
    try:
        await on_progress(
            phase=phase, percent=percent, message=message, run_id=run_id
        )
    except TypeError:
        await on_progress(phase=phase, percent=percent, message=message)


async def _apply_legacy_fields(
    store: BackupStore, run_id: int, hop: dict[str, Any]
) -> None:
    role = legacy_role_for(str(hop.get("kind") or ""), str(hop.get("preset") or ""))
    if not role:
        return
    fields: dict[str, Any] = {
        f"{role}_status": hop.get("status"),
        f"{role}_verify": hop.get("verify"),
    }
    if hop.get("path"):
        fields[f"{role}_path"] = hop["path"]
    await store.update_run(run_id, **fields)


def _hop_entry(
    dest: dict[str, Any],
    *,
    status: str,
    verify: str,
    path: str = "",
) -> dict[str, Any]:
    return {
        "id": dest.get("id"),
        "kind": dest.get("kind"),
        "label": dest.get("label") or dest.get("kind"),
        "preset": dest.get("preset") or "custom",
        "status": status,
        "verify": verify,
        "path": path,
    }


async def _write_password_file(
    settings: Settings,
    *,
    ip: str | None,
    local: bool,
    remote_path: str,
    password: str,
    timeout: float,
) -> None:
    data = password.encode("utf-8")
    if local:
        p = Path(remote_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.chmod(0o600)
        return
    assert ip is not None
    await sshutil.sftp_write_bytes(settings, ip, remote_path, data, mode=0o600)
    await sshutil.ssh_run(
        settings, ip, f"chmod 600 -- {shlex.quote(remote_path)}", timeout=timeout
    )


async def _remove_password_file(
    settings: Settings,
    *,
    ip: str | None,
    local: bool,
    remote_path: str,
    timeout: float,
) -> None:
    cmd = f"rm -f -- {shlex.quote(remote_path)}"
    try:
        if local:
            await sshutil.local_run(cmd, timeout=timeout)
        elif ip:
            await sshutil.ssh_run(settings, ip, cmd, timeout=timeout)
    except Exception:
        logger.debug("restic password file cleanup failed", exc_info=True)


def _local_restic_binary() -> Path | None:
    which = shutil.which("restic")
    candidates = [Path(which)] if which else []
    candidates.extend((Path("/usr/bin/restic"), Path("/usr/local/bin/restic")))
    for path in candidates:
        if path.is_file():
            return path
    return None


def _restic_pkg_install_script() -> str:
    """apt/apk install — runs detached; never under the short SSH probe timeout."""
    return r"""
set -euo pipefail
PROG="${HC_JOB_PROGRESS:-}"
note() { if [ -n "$PROG" ]; then printf '%s\n' "$1" > "$PROG"; fi; }

if command -v restic >/dev/null 2>&1; then
  note "restic bereits vorhanden"
  exit 0
fi

LOCK=0
for f in /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock \
  /var/lib/dpkg/lock /var/cache/apt/archives/lock; do
  [ -e "$f" ] || continue
  if command -v fuser >/dev/null 2>&1 && fuser "$f" >/dev/null 2>&1; then
    LOCK=1; break
  fi
  if command -v lsof >/dev/null 2>&1 && lsof -t "$f" >/dev/null 2>&1; then
    LOCK=1; break
  fi
done
if [ "$LOCK" = "1" ]; then
  echo "APT_LOCK=1" >&2
  echo "Could not get lock — another apt/unattended-upgrades process is running." >&2
  exit 2
fi

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive LC_ALL=C
  note "apt-get update …"
  apt-get update -qq \
    -o Acquire::Retries=1 \
    -o Acquire::http::Timeout=20 \
    -o Acquire::https::Timeout=20 \
    -o DPkg::Lock::Timeout=0
  note "apt-get install restic …"
  apt-get install -y -qq restic -o DPkg::Lock::Timeout=0
elif command -v apk >/dev/null 2>&1; then
  note "apk add restic …"
  apk add --no-cache restic
else
  echo "Kein apt/apk — restic kann nicht automatisch installiert werden." >&2
  exit 1
fi

if ! command -v restic >/dev/null 2>&1; then
  echo "restic: not found after install" >&2
  exit 1
fi
note "restic installiert"
"""


async def _probe_restic(
    settings: Settings,
    *,
    ip: str | None,
    local: bool,
    timeout: float,
) -> bool:
    check = (
        "command -v restic 2>/dev/null || "
        "{ [ -x /usr/local/bin/restic ] && echo /usr/local/bin/restic; }"
    )
    if local:
        out, _, code = await sshutil.local_run(check, timeout=min(15.0, timeout))
    else:
        assert ip is not None
        out, _, code = await sshutil.ssh_run(
            settings, ip, check, timeout=min(30.0, timeout)
        )
    return code == 0 and bool((out or "").strip())


async def _push_restic_binary(
    settings: Settings,
    ip: str,
    src: Path,
    *,
    transfer_timeout: float,
    short_timeout: float,
    log: LogFn,
) -> bool:
    dest = "/usr/local/bin/restic"
    await log(f"restic fehlt — kopiere Binary vom Copilot ({src}) nach {dest} …")
    await sshutil.ssh_run_ok(
        settings, ip, "mkdir -p -- /usr/local/bin", timeout=short_timeout
    )
    await sshutil.scp_put(
        settings, ip, src, dest, timeout=max(120.0, transfer_timeout)
    )
    # Non-login SSH PATH often omits /usr/local/bin — symlink into /usr/bin.
    out, err, code = await sshutil.ssh_run(
        settings,
        ip,
        f"chmod 755 -- {shlex.quote(dest)} && "
        f"{shlex.quote(dest)} version >/dev/null && "
        f"if ! command -v restic >/dev/null 2>&1; then "
        f"ln -sfn {shlex.quote(dest)} /usr/bin/restic; fi; "
        f"command -v restic",
        timeout=short_timeout,
    )
    if code != 0 or not (out or "").strip():
        logger.warning(
            "restic binary push verify failed on %s: %s",
            ip,
            (err or out or "").strip(),
        )
        return False
    return True


async def ensure_restic_installed(
    settings: Settings,
    *,
    ip: str | None,
    local: bool,
    allow_install: bool,
    timeout: float,
    log: LogFn,
    work_dir: str | None = None,
    install_timeout: float | None = None,
    transfer_timeout: float | None = None,
) -> None:
    """Ensure restic exists. Probe is short; install never shares that timeout.

    Remote: copy the Copilot image binary via SCP, then apt/apk via nohup+poll.
    """
    short = min(30.0, max(10.0, timeout))
    long_install = float(install_timeout or 600.0)
    xfer = float(transfer_timeout or 600.0)
    if await _probe_restic(settings, ip=ip, local=local, timeout=short):
        return
    if not allow_install:
        raise ResticError(
            "restic fehlt auf dem Host und RESTIC_INSTALL=false. "
            "restic bitte auf dem LXC installieren (apt/apk) oder "
            "RESTIC_INSTALL=true setzen (kopiert das Copilot-Binary)."
        )

    if local:
        await log("restic fehlt lokal — Installation per apt/apk …")
        try:
            await sshutil.local_run_ok(
                _restic_pkg_install_script(), timeout=long_install
            )
        except DockerControlError as exc:
            raise ResticError(
                "restic konnte nicht installiert werden: "
                + translate_restic_error(exc.message)
            ) from exc
        if not await _probe_restic(
            settings, ip=ip, local=True, timeout=short
        ):
            raise ResticError(
                "restic fehlt nach der Installation (nicht im PATH)."
            )
        await log("restic ist installiert.")
        return

    assert ip is not None
    src = _local_restic_binary()
    if src is not None:
        try:
            if await _push_restic_binary(
                settings,
                ip,
                src,
                transfer_timeout=xfer,
                short_timeout=short,
                log=log,
            ):
                await log("restic ist installiert (Binary vom Copilot).")
                return
            await log(
                "Binary-Kopie unvollständig — versuche apt/apk im Hintergrund …"
            )
        except DockerControlError as exc:
            logger.warning("restic binary push to %s failed: %s", ip, exc.message)
            await log(
                "Binary-Kopie fehlgeschlagen — versuche apt/apk im Hintergrund …"
            )
    else:
        await log(
            "restic fehlt im Copilot-Image — Installation per apt/apk "
            "(Hintergrund, langer Timeout) …"
        )

    job_dir = (work_dir or "/tmp/homelab-copilot-restic").rstrip("/")
    try:
        await sshutil.run_detached_and_poll(
            settings,
            ip,
            _restic_pkg_install_script(),
            work_dir=job_dir,
            job_name="restic_install",
            local=False,
            overall_timeout=long_install,
            poll_interval=5.0,
            short_timeout=short,
            log=log,
            progress_label="restic-Install",
            job_label="restic-Installation",
        )
    except DockerControlError as exc:
        raise ResticError(
            "restic konnte nicht installiert werden: "
            + translate_restic_error(exc.message)
        ) from exc

    if not await _probe_restic(settings, ip=ip, local=False, timeout=short):
        raise ResticError(
            "restic fehlt nach der Installation (nicht im PATH). "
            "Paketquelle prüfen oder restic manuell installieren."
        )
    await log("restic ist installiert.")


def _build_restic_script(
    *,
    inventory: dict[str, Any],
    repo: str,
    password_file: str,
    cache_dir: str,
    work_dir: str,
    files_from: str,
    progress_path: str,
    do_full: bool,
    keep_last: int,
    keep_weekly: int,
    tag_kind: str,
) -> str:
    project = inventory["stack"]
    wd = inventory.get("working_dir") or ""
    volume_names = [
        v["name"] for v in (inventory.get("named_volumes") or []) if v.get("name")
    ]
    vol_lines = []
    for name in volume_names:
        safe = safe_name(name)
        vol_lines.append(
            f'MP=$(docker volume inspect -f "{{{{.Mountpoint}}}}" {shlex.quote(name)} 2>/dev/null || true)\n'
            f'if [ -n "$MP" ] && [ -d "$MP" ]; then\n'
            f'  printf "%s\\n" "$MP" >> {shlex.quote(files_from)}\n'
            f'else\n'
            f'  STAGE={shlex.quote(work_dir + "/volstage/" + safe)}\n'
            f'  mkdir -p "$STAGE"\n'
            f'  docker run --rm -v {shlex.quote(name)}:/v:ro -v "$STAGE:/out" '
            f'alpine:3.20 sh -c "cp -a /v/. /out/" 2>/dev/null || '
            f'  docker run --rm -v {shlex.quote(name)}:/v:ro -v "$STAGE:/out" '
            f'busybox:1.36 sh -c "cp -a /v/. /out/" || true\n'
            f'  if [ -d "$STAGE" ]; then printf "%s\\n" "$STAGE" >> {shlex.quote(files_from)}; fi\n'
            f'fi'
        )

    compose_dump = ""
    if wd:
        compose_dump = (
            f"mkdir -p {shlex.quote(work_dir + '/meta')}\n"
            f"(cd {shlex.quote(wd)} && docker compose config) "
            f"> {shlex.quote(work_dir + '/meta/compose-config.yml')} 2>/dev/null || "
            f"docker compose -p {shlex.quote(project)} config "
            f"> {shlex.quote(work_dir + '/meta/compose-config.yml')} 2>/dev/null || true\n"
            f"if [ -f {shlex.quote(work_dir + '/meta/compose-config.yml')} ]; then "
            f"printf '%s\\n' {shlex.quote(work_dir + '/meta/compose-config.yml')} "
            f">> {shlex.quote(files_from)}; fi\n"
        )

    full_block = ""
    if do_full:
        weekly = f"--keep-weekly {int(keep_weekly)}" if keep_weekly > 0 else ""
        full_block = f"""
{_prog_line(progress_path, "forget/prune")}
restic unlock >/dev/null 2>&1 || true
restic forget --keep-last {int(keep_last)} {weekly} --prune --json \\
  > {shlex.quote(work_dir + "/restic.forget")} 2>> {shlex.quote(work_dir + "/restic.err")} || true
{_prog_line(progress_path, "check")}
restic check >> {shlex.quote(work_dir + "/restic.err")} 2>&1
"""

    return f"""
set -euo pipefail
export RESTIC_REPOSITORY={shlex.quote(repo)}
export RESTIC_PASSWORD_FILE={shlex.quote(password_file)}
export RESTIC_CACHE_DIR={shlex.quote(cache_dir)}
WORK={shlex.quote(work_dir)}
FF={shlex.quote(files_from)}
PROG={shlex.quote(progress_path)}
mkdir -p "$WORK" {shlex.quote(cache_dir)} {shlex.quote(repo)}
{_prog_line(progress_path, "prepare")}
if ! command -v restic >/dev/null 2>&1; then
  echo "restic: not found" >&2
  exit 127
fi
if [ ! -f "$RESTIC_REPOSITORY/config" ]; then
  {_prog_line(progress_path, "init")}
  restic init >> {shlex.quote(work_dir + "/restic.err")} 2>&1
fi
restic unlock >/dev/null 2>&1 || true
{chr(10).join(vol_lines)}
{compose_dump}
{_prog_line(progress_path, "backup")}
set +e
restic backup --json --files-from "$FF" \\
  --exclude {shlex.quote(repo)} \\
  --exclude {shlex.quote(cache_dir)} \\
  --tag homelab-copilot \\
  --tag {shlex.quote("stack:" + project)} \\
  --tag {shlex.quote(tag_kind)} \\
  > {shlex.quote(work_dir + "/restic.out")} 2>> {shlex.quote(work_dir + "/restic.err")} &
RPID=$!
while kill -0 "$RPID" 2>/dev/null; do
  if [ -f {shlex.quote(work_dir + "/restic.out")} ]; then
    line=$(tail -n 8 {shlex.quote(work_dir + "/restic.out")} | grep '"percent_done"' | tail -1 || true)
    if [ -n "$line" ]; then
      pct=$(printf '%s' "$line" | sed -n 's/.*"percent_done":\\([0-9.]*\\).*/\\1/p')
      if [ -n "$pct" ]; then
        awk -v p="$pct" 'BEGIN {{ printf "restic %d%%\\n", p*100 }}' > "$PROG"
      fi
    fi
  fi
  sleep 5
done
wait "$RPID"
EC=$?
set -e
grep '"message_type":"summary"' {shlex.quote(work_dir + "/restic.out")} | tail -1 \\
  > {shlex.quote(work_dir + "/.restic_result")} || true
if [ "$EC" -ne 0 ] && [ "$EC" -ne 3 ]; then
  tail -n 30 {shlex.quote(work_dir + "/restic.err")} >&2 || true
  exit "$EC"
fi
{full_block}
{_prog_line(progress_path, "done")}
printf '%s\\n' "$EC" > {shlex.quote(work_dir + "/.restic_exit")}
"""


def _prog_line(progress_path: str, msg: str) -> str:
    return f'printf "%s\\n" {shlex.quote(msg)} > {shlex.quote(progress_path)}'


def _parse_summary(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    snap = str(data.get("snapshot_id") or data.get("snapshot") or "")
    return {
        "snapshot_id": snap,
        "bytes_added": int(data.get("data_added") or data.get("bytes_added") or 0),
        "bytes_processed": int(
            data.get("total_bytes_processed") or data.get("bytes_processed") or 0
        ),
        "files_new": data.get("files_new"),
        "files_changed": data.get("files_changed"),
    }


async def _read_remote_text(
    settings: Settings,
    *,
    ip: str | None,
    local: bool,
    path: str,
    timeout: float = 30.0,
) -> str:
    cmd = f"cat {shlex.quote(path)} 2>/dev/null || true"
    if local:
        out, _, _ = await sshutil.local_run(cmd, timeout=timeout)
    else:
        assert ip is not None
        out, _, _ = await sshutil.ssh_run(settings, ip, cmd, timeout=timeout)
    return out or ""


async def _mirror_lxc_to_copilot(
    settings: Settings,
    *,
    ip: str | None,
    local: bool,
    lxc_repo: str,
    copilot_repo: Path,
    timeout: float,
    log: LogFn,
) -> None:
    copilot_repo.mkdir(parents=True, exist_ok=True)
    if local:
        await sshutil.local_run_ok(
            f"mkdir -p -- {shlex.quote(str(copilot_repo))} && "
            f"rsync -a --delete --exclude locks "
            f"{shlex.quote(lxc_repo.rstrip('/') + '/')} "
            f"{shlex.quote(str(copilot_repo) + '/')} "
            f"|| cp -a {shlex.quote(lxc_repo + '/.')} {shlex.quote(str(copilot_repo) + '/')}",
            timeout=timeout,
        )
        await log(f"Repo nach Copilot gespiegelt: {copilot_repo}")
        return
    assert ip is not None
    used_rsync = False
    try:
        used_rsync = await sshutil.rsync_pull(
            settings, ip, lxc_repo, copilot_repo, timeout=timeout
        )
    except DockerControlError as exc:
        await log(f"rsync fehlgeschlagen — SFTP-Fallback ({exc.message[:120]})")
        used_rsync = False
    if not used_rsync:
        await log(
            "rsync auf dem Guest fehlt — spiegele das Repo per SFTP nach Copilot…"
        )
        stats = await sshutil.sftp_mirror_get(
            settings,
            ip,
            lxc_repo,
            copilot_repo,
            timeout=timeout,
            log=log,
            label="Copilot",
        )
        await log(
            f"Repo per SFTP nach Copilot: {stats['copied']} neu, "
            f"{stats['skipped']} unverändert, {stats['deleted']} entfernt"
        )
    else:
        await log(f"Repo per rsync nach Copilot: {copilot_repo}")


async def _mirror_copilot_to_lxc(
    settings: Settings,
    *,
    ip: str | None,
    local: bool,
    lxc_repo: str,
    copilot_repo: Path,
    timeout: float,
    log: LogFn,
) -> None:
    if not (copilot_repo / "config").is_file():
        return
    if local:
        await sshutil.local_run_ok(
            f"mkdir -p -- {shlex.quote(lxc_repo)} && "
            f"rsync -a --delete --exclude locks "
            f"{shlex.quote(str(copilot_repo) + '/')} "
            f"{shlex.quote(lxc_repo.rstrip('/') + '/')} "
            f"|| cp -a {shlex.quote(str(copilot_repo) + '/.')} {shlex.quote(lxc_repo + '/')}",
            timeout=timeout,
        )
        return
    assert ip is not None
    await sshutil.ensure_remote_dir(settings, ip, lxc_repo, timeout=30)
    used_rsync = False
    try:
        used_rsync = await sshutil.rsync_push(
            settings, ip, copilot_repo, lxc_repo, timeout=timeout
        )
    except DockerControlError:
        used_rsync = False
    if not used_rsync:
        await sshutil.sftp_mirror_put(
            settings,
            ip,
            copilot_repo,
            lxc_repo,
            timeout=timeout,
            log=log,
            label="Host",
        )
    await log("Copilot-Repo auf den Host zurückgespiegelt (Incrementals fortsetzen).")


async def _mirror_to_sftp(
    settings: Settings,
    dest: dict[str, Any],
    copilot_repo: Path,
    *,
    parent_id: str,
    project: str,
    timeout: float,
    log: LogFn,
    on_progress: ProgressFn | None = None,
    run_id: int | None = None,
    phase: str | None = None,
    percent_start: int = 70,
    percent_end: int = 92,
) -> str:
    if not (copilot_repo / "config").is_file():
        raise ResticError("Copilot-restic-Repo fehlt — SFTP-Kopie nicht möglich.")
    host = (dest.get("host") or "").strip()
    remote_base = (dest.get("remote_path") or "").rstrip("/")
    if not host or not remote_base:
        raise ResticError("SFTP-Ziel: Host/Pfad unvollständig.")
    remote = f"{remote_base}/{sftp_repo_rel(parent_id, project)}"
    auth = resolve_auth(dest, settings)
    hop_label = str(dest.get("label") or "SFTP")
    await log(f"Spiegele restic-Repo nach {hop_label}: {remote}")

    async def hop_progress(message: str, file_pct: int) -> None:
        span = max(1, int(percent_end) - int(percent_start))
        mapped = int(percent_start) + int(span * max(0, min(100, file_pct)) / 100)
        await _emit_progress(
            on_progress,
            phase=phase or f"→ {hop_label}",
            percent=mapped,
            message=message,
            run_id=run_id,
        )

    stats = await sshutil.sftp_mirror_put(
        settings,
        host,
        copilot_repo,
        remote,
        timeout=timeout,
        username=auth["username"],
        key=auth.get("key"),
        key_pem=auth.get("key_pem"),
        password=auth.get("password"),
        port=auth["port"],
        log=log,
        label=hop_label,
        on_progress=hop_progress,
    )
    await log(
        f"{hop_label}: {stats['copied']} neu, "
        f"{stats['skipped']} unverändert, {stats['deleted']} entfernt"
    )
    return remote


async def _mirror_from_sftp(
    settings: Settings,
    dest: dict[str, Any],
    copilot_repo: Path,
    *,
    parent_id: str,
    project: str,
    timeout: float,
    log: LogFn,
) -> None:
    host = (dest.get("host") or "").strip()
    remote_base = (dest.get("remote_path") or "").rstrip("/")
    remote = f"{remote_base}/{sftp_repo_rel(parent_id, project)}"
    auth = resolve_auth(dest, settings)
    await log(f"Hole restic-Repo von {dest.get('label')}: {remote}")
    copilot_repo.mkdir(parents=True, exist_ok=True)
    await sshutil.sftp_mirror_get(
        settings,
        host,
        remote,
        copilot_repo,
        timeout=timeout,
        username=auth["username"],
        key=auth.get("key"),
        key_pem=auth.get("key_pem"),
        password=auth.get("password"),
        port=auth["port"],
        log=log,
        label=str(dest.get("label") or "SFTP"),
    )


async def run_restic_backup(
    store: BackupStore,
    *,
    run_id: int,
    parent_id: str,
    project: str,
    inventory: dict[str, Any],
    settings: Settings,
    bsettings: BackupSettings,
    quiesce: bool,
    pipeline: list[dict[str, Any]],
    full_every_days: int,
    keep_last: int,
    keep_weekly: int,
    on_progress: ProgressFn | None,
    log: LogFn,
) -> dict[str, Any]:
    local = bool(inventory["local"])
    ip = inventory["host_ip"]
    timeout = bsettings.backup_ssh_timeout
    archive_timeout = bsettings.backup_archive_timeout
    transfer_timeout = bsettings.backup_transfer_timeout
    durable = [d for d in pipeline if d.get("kind") != KIND_HOST]
    if not durable:
        raise ResticError("Keine dauerhaften Backup-Ziele aktiv (Copilot/SFTP).")

    password = await store.get_or_create_restic_password(parent_id, project)
    meta = await store.get_restic_secret_meta(parent_id, project)
    do_full = _should_run_full(
        (meta or {}).get("last_full_at_iso"), int(full_every_days or 7)
    )
    tag_kind = "full" if do_full else "incr"

    if local:
        base = str(bsettings.copilot_dir / "_staging")
    else:
        base = bsettings.backup_lxc_dir.rstrip("/")
    lxc_repo = lxc_repo_path(base if local else bsettings.backup_lxc_dir, project)
    if local:
        lxc_repo = str(copilot_repo_path(bsettings.copilot_dir, parent_id, project))
    copilot_repo = copilot_repo_path(bsettings.copilot_dir, parent_id, project)
    work_dir = f"{base}/restic-work/{safe_name(project)}"
    cache_dir = f"{base}/restic-cache/{safe_name(project)}"
    password_file = f"{work_dir}/.restic-pass"
    files_from = f"{work_dir}/files-from.txt"
    progress_path = f"{work_dir}/.hc_job_restic.progress"

    hop_results: list[dict[str, Any]] = []
    started = False
    stopped = False
    snapshot_id = ""
    bytes_added = 0
    bytes_processed = 0

    paths = collect_restic_paths(inventory)
    explicit = [p["path"] for p in paths if p["path"] and "/" in p["path"]]
    if not explicit and not (inventory.get("named_volumes") or []):
        raise ResticError(
            "Nichts zu sichern — keine Compose-Dateien, Volumes oder lesbaren Binds."
        )

    try:
        await _emit_progress(
            on_progress,
            phase="restic",
            percent=10,
            message="restic prüfen / installieren…",
            run_id=run_id,
        )
        await ensure_restic_installed(
            settings,
            ip=ip,
            local=local,
            allow_install=bsettings.restic_install,
            timeout=timeout,
            log=log,
            work_dir=work_dir,
            install_timeout=bsettings.restic_install_timeout,
            transfer_timeout=transfer_timeout,
        )

        await _emit_progress(
            on_progress,
            phase="Repo",
            percent=14,
            message="Repository vorbereiten…",
            run_id=run_id,
        )
        if local:
            await sshutil.local_run_ok(
                f"mkdir -p -- {shlex.quote(work_dir)} {shlex.quote(cache_dir)} "
                f"{shlex.quote(lxc_repo)}",
                timeout=30,
            )
        else:
            assert ip is not None
            await sshutil.ensure_remote_dir(settings, ip, work_dir, timeout=30)
            await sshutil.ensure_remote_dir(settings, ip, cache_dir, timeout=30)
            await sshutil.ensure_remote_dir(settings, ip, lxc_repo, timeout=30)

        # Resume incrementals if LXC repo is empty but Copilot has a copy
        lxc_has_config = False
        check_cfg = f"test -f {shlex.quote(lxc_repo + '/config')}"
        if local:
            _, _, ccode = await sshutil.local_run(check_cfg, timeout=10)
            lxc_has_config = ccode == 0
        else:
            assert ip is not None
            _, _, ccode = await sshutil.ssh_run(
                settings, ip, check_cfg, timeout=15
            )
            lxc_has_config = ccode == 0
        if not lxc_has_config and (copilot_repo / "config").is_file() and not local:
            await _mirror_copilot_to_lxc(
                settings,
                ip=ip,
                local=local,
                lxc_repo=lxc_repo,
                copilot_repo=copilot_repo,
                timeout=transfer_timeout,
                log=log,
            )

        if local:
            Path(files_from).parent.mkdir(parents=True, exist_ok=True)
            Path(files_from).write_text(
                "\n".join(explicit) + ("\n" if explicit else ""),
                encoding="utf-8",
            )
        else:
            assert ip is not None
            body = ("\n".join(explicit) + "\n").encode("utf-8")
            await sshutil.sftp_write_bytes(settings, ip, files_from, body, mode=0o600)

        await _write_password_file(
            settings,
            ip=ip,
            local=local,
            remote_path=password_file,
            password=password,
            timeout=timeout,
        )

        if quiesce:
            await _emit_progress(
                on_progress,
                phase="Quiesce",
                percent=18,
                message="Stack wird gestoppt (Quiesce)…",
                run_id=run_id,
            )
            await log("Quiesce: docker compose stop …")
            await _compose_stop(
                settings, ip, project, inventory, local=local, timeout=timeout
            )
            stopped = True
        else:
            await log("Quiesce deaktiviert — Volumes werden live gesichert")

        await _emit_progress(
            on_progress,
            phase="restic backup",
            percent=25,
            message="restic backup läuft (erster Lauf kann lange dauern)…",
            run_id=run_id,
        )
        if do_full:
            await log(
                f"Full-Tag: forget/prune (keep-last {keep_last}, "
                f"keep-weekly {keep_weekly}) und restic check nach dem Snapshot"
            )
        else:
            await log("Inkrementeller Snapshot (kein prune heute)")

        script = _build_restic_script(
            inventory=inventory,
            repo=lxc_repo,
            password_file=password_file,
            cache_dir=cache_dir,
            work_dir=work_dir,
            files_from=files_from,
            progress_path=progress_path,
            do_full=do_full,
            keep_last=keep_last,
            keep_weekly=keep_weekly,
            tag_kind=tag_kind,
        )
        try:
            await sshutil.run_detached_and_poll(
                settings,
                ip,
                script,
                work_dir=work_dir,
                job_name="restic",
                local=local,
                overall_timeout=archive_timeout,
                poll_interval=5.0,
                short_timeout=min(60.0, timeout),
                log=log,
                progress_label="restic",
            )
        except DockerControlError as exc:
            raise ResticError(translate_restic_error(exc.message)) from exc

        summary_raw = await _read_remote_text(
            settings, ip=ip, local=local, path=f"{work_dir}/.restic_result"
        )
        summary = _parse_summary(summary_raw)
        snapshot_id = str(summary.get("snapshot_id") or "")
        bytes_added = int(summary.get("bytes_added") or 0)
        bytes_processed = int(summary.get("bytes_processed") or 0)
        if not snapshot_id:
            err_tail = await _read_remote_text(
                settings, ip=ip, local=local, path=f"{work_dir}/restic.err"
            )
            raise ResticError(
                "restic hat keinen Snapshot erzeugt. "
                + translate_restic_error(err_tail)
            )

        manifest = {
            "engine": ENGINE_RESTIC,
            "stack": project,
            "parent_id": parent_id,
            "guest_name": inventory.get("guest_name"),
            "created_at": format_de(now_berlin()),
            "created_at_iso": iso_utc(now_berlin()),
            "snapshot_id": snapshot_id,
            "tag": tag_kind,
            "full": do_full,
            "paths": paths,
            "quiesced": quiesce,
            "warnings": inventory.get("warnings") or [],
            "gaps": inventory.get("gaps") or [],
            "bytes_added": bytes_added,
            "bytes_processed": bytes_processed,
            "working_dir": inventory.get("working_dir"),
            "archive_sha256": "0" * 64,
            "mounts": [
                {"type": "volume", **v} for v in (inventory.get("named_volumes") or [])
            ]
            + [{"type": "bind", **b} for b in (inventory.get("bind_mounts") or [])],
        }
        await store.update_run(
            run_id,
            archive_name=f"restic:{snapshot_id[:12]}",
            archive_sha256=snapshot_id if len(snapshot_id) >= 16 else snapshot_id.ljust(64, "0"),
            snapshot_id=snapshot_id,
            bytes_added=bytes_added,
            bytes_processed=bytes_processed,
            size_bytes=bytes_added,
            manifest_json=manifest,
        )
        await log(
            f"Snapshot {snapshot_id[:12]}… · +{bytes_added} Bytes "
            f"(gescannt {bytes_processed})"
        )

        host_dest = next((d for d in pipeline if d.get("kind") == KIND_HOST), None)
        hop = _hop_entry(
            host_dest or {"kind": KIND_HOST, "label": "Host-Repo"},
            status="ok",
            verify="ok",
            path=lxc_repo,
        )
        hop_results.append(hop)
        await _apply_legacy_fields(store, run_id, hop)
        await store.update_run(run_id, destinations_json=hop_results)

        if stopped:
            await log("Stack wieder starten …")
            await _compose_up(
                settings, ip, project, inventory, local=local, timeout=timeout
            )
            started = True
            stopped = False

        if do_full:
            await store.mark_restic_full(parent_id, project)

        n_dur = len(durable)
        for idx, dest in enumerate(durable):
            kind = dest.get("kind")
            label = dest.get("label") or kind
            pct = 70 + int(22 * (idx / max(n_dur, 1)))
            pct_end = 70 + int(22 * ((idx + 1) / max(n_dur, 1)))
            await _emit_progress(
                on_progress,
                phase=f"→ {label}",
                percent=pct,
                message=f"restic-Repo nach {label} spiegeln…",
                run_id=run_id,
            )
            try:
                if kind == KIND_COPILOT:
                    if local:
                        # Repo already lives under Copilot path
                        dest_path = lxc_repo
                        hop = _hop_entry(
                            dest, status="ok", verify="ok", path=dest_path
                        )
                    else:
                        await _mirror_lxc_to_copilot(
                            settings,
                            ip=ip,
                            local=local,
                            lxc_repo=lxc_repo,
                            copilot_repo=copilot_repo,
                            timeout=transfer_timeout,
                            log=log,
                        )
                        dest_path = str(copilot_repo)
                        ok = (copilot_repo / "config").is_file()
                        hop = _hop_entry(
                            dest,
                            status="ok" if ok else "failed",
                            verify="ok" if ok else "failed",
                            path=dest_path,
                        )
                        if not ok:
                            hop_results.append(hop)
                            await _apply_legacy_fields(store, run_id, hop)
                            await store.update_run(run_id, destinations_json=hop_results)
                            raise ResticError(
                                f"{label}: restic-Repo nach Sync unvollständig."
                            )
                    hop_results.append(hop)
                    await _apply_legacy_fields(store, run_id, hop)
                    await store.update_run(run_id, destinations_json=hop_results)
                    if hop_results and hop_results[0].get("kind") == KIND_HOST:
                        hop_results[0]["status"] = "cleared"
                        await _apply_legacy_fields(store, run_id, hop_results[0])
                        await store.update_run(run_id, destinations_json=hop_results)

                elif kind == KIND_SFTP:
                    if not (copilot_repo / "config").is_file() and not local:
                        raise ResticError(
                            f"{label}: Copilot-Repo fehlt — Copilot-Ziel muss "
                            "vor SFTP in der Pipeline stehen."
                        )
                    src_repo = copilot_repo if (copilot_repo / "config").is_file() else Path(lxc_repo)
                    remote = await _mirror_to_sftp(
                        settings,
                        dest,
                        src_repo,
                        parent_id=parent_id,
                        project=project,
                        timeout=transfer_timeout,
                        log=log,
                        on_progress=on_progress,
                        run_id=run_id,
                        phase=f"→ {label}",
                        percent_start=pct,
                        percent_end=pct_end,
                    )
                    hop = _hop_entry(dest, status="ok", verify="ok", path=remote)
                    hop_results.append(hop)
                    await _apply_legacy_fields(store, run_id, hop)
                    await store.update_run(run_id, destinations_json=hop_results)
                else:
                    hop = _hop_entry(dest, status="skipped", verify="skipped")
                    hop_results.append(hop)
                    await store.update_run(run_id, destinations_json=hop_results)
            except ResticError:
                if kind == KIND_COPILOT:
                    raise
                await log(f"{label} fehlgeschlagen — Run wird partial")
                hop = _hop_entry(dest, status="failed", verify="failed")
                hop_results.append(hop)
                await _apply_legacy_fields(store, run_id, hop)
                await store.update_run(run_id, destinations_json=hop_results)
            except Exception as exc:
                msg = getattr(exc, "message", None) or str(exc)
                if kind == KIND_COPILOT:
                    hop = _hop_entry(dest, status="failed", verify="failed")
                    hop_results.append(hop)
                    await _apply_legacy_fields(store, run_id, hop)
                    await store.update_run(run_id, destinations_json=hop_results)
                    raise ResticError(f"{label} fehlgeschlagen: {msg}") from exc
                await log(f"{label} fehlgeschlagen: {msg}")
                hop = _hop_entry(dest, status="failed", verify="failed")
                hop_results.append(hop)
                await _apply_legacy_fields(store, run_id, hop)
                await store.update_run(run_id, destinations_json=hop_results)

        from backup_verifier.verify import summarize_hop_verifies

        verify_status, verify_detail = summarize_hop_verifies(hop_results)
        copilot_ok = any(
            h.get("kind") == KIND_COPILOT and h.get("status") == "ok"
            for h in hop_results
        )
        remote_failed = any(
            h.get("kind") == KIND_SFTP and h.get("status") == "failed"
            for h in hop_results
        )
        if not copilot_ok:
            final_status = "failed"
        elif remote_failed:
            final_status = "partial"
        else:
            final_status = "success"

        now = now_berlin()
        await store.update_run(
            run_id,
            status=final_status,
            finished_at=format_de(now),
            finished_at_iso=iso_utc(now),
            verify_status=verify_status,
            verify_detail=verify_detail,
            destinations_json=hop_results,
            snapshot_id=snapshot_id,
            bytes_added=bytes_added,
            bytes_processed=bytes_processed,
        )
        await log(f"Fertig — Status: {final_status} · Snapshot {snapshot_id[:12]}")
        await _emit_progress(
            on_progress,
            phase="Fertig",
            percent=100,
            message=f"Fertig — {final_status} · {snapshot_id[:12]}",
            run_id=run_id,
        )
        return await store.get_run(run_id) or {"id": run_id, "status": final_status}

    except Exception as exc:
        msg = getattr(exc, "message", None) or str(exc)
        if not isinstance(exc, ResticError):
            msg = translate_restic_error(msg)
        await log(f"Fehler: {msg}")
        now = now_berlin()
        from backup_verifier.verify import summarize_hop_verifies

        verify_status, verify_detail = summarize_hop_verifies(hop_results)
        await store.update_run(
            run_id,
            status="failed",
            finished_at=format_de(now),
            finished_at_iso=iso_utc(now),
            error_message=msg,
            destinations_json=hop_results,
            verify_status=verify_status,
            verify_detail=verify_detail,
            snapshot_id=snapshot_id or None,
            bytes_added=bytes_added or None,
            bytes_processed=bytes_processed or None,
        )
        if not any(h.get("kind") == KIND_HOST for h in hop_results):
            await store.update_run(run_id, lxc_status="failed")
        await _emit_progress(
            on_progress,
            phase="Fehler",
            percent=100,
            message=msg,
            run_id=run_id,
        )
        if isinstance(exc, ResticError):
            raise
        raise ResticError(msg) from exc
    finally:
        await _remove_password_file(
            settings,
            ip=ip,
            local=local,
            remote_path=password_file,
            timeout=timeout,
        )
        if stopped and not started:
            try:
                await log("Notfall: Stack nach Fehler wieder starten …")
                await _compose_up(
                    settings, ip, project, inventory, local=local, timeout=timeout
                )
            except Exception:
                logger.exception("Failed to restart stack after restic backup error")


async def _compose_stop(
    settings: Settings,
    ip: str | None,
    project: str,
    inventory: dict[str, Any],
    *,
    local: bool,
    timeout: float,
) -> None:
    wd = inventory.get("working_dir")
    if wd:
        cmd = f"cd {shlex.quote(wd)} && docker compose stop"
    else:
        cmd = f"docker compose -p {shlex.quote(project)} stop"
    if local:
        await sshutil.local_run_ok(cmd, timeout=timeout)
    else:
        assert ip is not None
        await sshutil.ssh_run_ok(settings, ip, cmd, timeout=timeout)


async def _compose_up(
    settings: Settings,
    ip: str | None,
    project: str,
    inventory: dict[str, Any],
    *,
    local: bool,
    timeout: float,
) -> None:
    wd = inventory.get("working_dir")
    if wd:
        cmd = f"cd {shlex.quote(wd)} && docker compose up -d"
    else:
        cmd = f"docker compose -p {shlex.quote(project)} start"
    if local:
        await sshutil.local_run_ok(cmd, timeout=timeout)
    else:
        assert ip is not None
        await sshutil.ssh_run_ok(settings, ip, cmd, timeout=timeout)


def _snapshot_rows(raw: str) -> list[dict[str, Any]]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("short_id") or (item.get("id") or "")[:8])
        full_id = str(item.get("id") or "")
        time_s = str(item.get("time") or "")
        tags = item.get("tags") or []
        paths = item.get("paths") or []
        out.append(
            {
                "id": full_id,
                "short_id": sid,
                "time": time_s,
                "hostname": item.get("hostname") or "",
                "tags": tags if isinstance(tags, list) else [],
                "paths": paths if isinstance(paths, list) else [],
            }
        )
    out.sort(key=lambda x: str(x.get("time") or ""), reverse=True)
    return out


async def list_restic_snapshots(
    store: BackupStore,
    *,
    parent_id: str,
    project: str,
    inventory: dict[str, Any],
    settings: Settings | None = None,
    bsettings: BackupSettings | None = None,
    source: str = "copilot",
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    bsettings = bsettings or get_backup_settings()
    password = await store.get_restic_password(parent_id, project)
    if not password:
        return []
    local = bool(inventory.get("local"))
    ip = inventory.get("host_ip")
    timeout = bsettings.backup_ssh_timeout
    copilot_repo = copilot_repo_path(bsettings.copilot_dir, parent_id, project)
    lxc_repo = lxc_repo_path(bsettings.backup_lxc_dir, project)
    if local:
        lxc_repo = str(copilot_repo)

    # Prefer Copilot copy; fall back to LXC; optionally pull SFTP first
    if str(source).isdigit() or source in ("synology", KIND_SFTP):
        await _ensure_sftp_repo_local(
            store, source=source, parent_id=parent_id, project=project,
            settings=settings, bsettings=bsettings,
        )

    repo = lxc_repo
    run_local = local
    if (copilot_repo / "config").is_file() and await _local_restic_available():
        repo = str(copilot_repo)
        run_local = True
    elif not local:
        run_local = False
        repo = lxc_repo

    work = copilot_repo.parent / "_snap"
    pw_file = str(work / f".pass-{safe_name(project)}")
    if run_local:
        work.mkdir(parents=True, exist_ok=True)
        Path(pw_file).write_text(password, encoding="utf-8")
        Path(pw_file).chmod(0o600)
        try:
            out, err, code = await sshutil.local_run(
                f"RESTIC_REPOSITORY={shlex.quote(repo)} "
                f"RESTIC_PASSWORD_FILE={shlex.quote(pw_file)} "
                f"restic snapshots --json --tag {shlex.quote('stack:' + project)} "
                f"2>/dev/null || RESTIC_REPOSITORY={shlex.quote(repo)} "
                f"RESTIC_PASSWORD_FILE={shlex.quote(pw_file)} restic snapshots --json",
                timeout=min(120.0, timeout),
            )
        finally:
            Path(pw_file).unlink(missing_ok=True)
        if code != 0:
            raise ResticError(translate_restic_error(err or out))
        return _snapshot_rows(out)

    assert ip is not None
    await ensure_restic_installed(
        settings,
        ip=ip,
        local=False,
        allow_install=bsettings.restic_install,
        timeout=timeout,
        log=_noop_log,
        work_dir=f"{bsettings.backup_lxc_dir.rstrip('/')}/restic-work/{safe_name(project)}",
        install_timeout=bsettings.restic_install_timeout,
        transfer_timeout=bsettings.backup_transfer_timeout,
    )
    remote_pw = f"{bsettings.backup_lxc_dir.rstrip('/')}/restic-work/{safe_name(project)}/.list-pass"
    await _write_password_file(
        settings, ip=ip, local=False, remote_path=remote_pw, password=password, timeout=timeout
    )
    try:
        out = await sshutil.ssh_run_ok(
            settings,
            ip,
            f"RESTIC_REPOSITORY={shlex.quote(repo)} "
            f"RESTIC_PASSWORD_FILE={shlex.quote(remote_pw)} "
            f"restic snapshots --json --tag {shlex.quote('stack:' + project)} "
            f"|| RESTIC_REPOSITORY={shlex.quote(repo)} "
            f"RESTIC_PASSWORD_FILE={shlex.quote(remote_pw)} restic snapshots --json",
            timeout=min(120.0, timeout),
        )
    except DockerControlError as exc:
        raise ResticError(translate_restic_error(exc.message)) from exc
    finally:
        await _remove_password_file(
            settings, ip=ip, local=False, remote_path=remote_pw, timeout=timeout
        )
    return _snapshot_rows(out)


async def _noop_log(_msg: str) -> None:
    return None


async def _local_restic_available() -> bool:
    _, _, code = await sshutil.local_run("command -v restic", timeout=10)
    return code == 0


async def _ensure_sftp_repo_local(
    store: BackupStore,
    *,
    source: str,
    parent_id: str,
    project: str,
    settings: Settings,
    bsettings: BackupSettings,
) -> None:
    from backup_verifier.destinations import ensure_seeded

    await ensure_seeded(store)
    dest = None
    if str(source).isdigit():
        dest = await store.get_destination(int(source))
    else:
        rows = await store.list_destinations()
        dest = next((d for d in rows if d.get("kind") == KIND_SFTP), None)
    if not dest or dest.get("kind") != KIND_SFTP:
        return
    copilot_repo = copilot_repo_path(bsettings.copilot_dir, parent_id, project)
    await _mirror_from_sftp(
        settings,
        dest,
        copilot_repo,
        parent_id=parent_id,
        project=project,
        timeout=bsettings.backup_transfer_timeout,
        log=_noop_log,
    )


async def run_restic_restore(
    store: BackupStore,
    *,
    restore_id: int,
    parent_id: str,
    project: str,
    snapshot_id: str,
    source: str,
    inventory: dict[str, Any],
    settings: Settings,
    bsettings: BackupSettings,
    log: LogFn,
) -> None:
    password = await store.get_restic_password(parent_id, project)
    if not password:
        raise ResticError(
            "Kein restic-Passwort für diesen Stack gespeichert. "
            "Zuerst ein Incremental-Backup ausführen."
        )
    snap = (snapshot_id or "").strip()
    if not snap:
        raise ResticError("Kein Snapshot gewählt.")
    if not re.fullmatch(r"[0-9a-fA-F]{8,64}", snap):
        raise ResticError("Ungültige Snapshot-ID.")

    local = bool(inventory["local"])
    ip = inventory["host_ip"]
    timeout = bsettings.backup_ssh_timeout
    archive_timeout = bsettings.backup_archive_timeout
    transfer_timeout = bsettings.backup_transfer_timeout
    copilot_repo = copilot_repo_path(bsettings.copilot_dir, parent_id, project)
    lxc_repo = lxc_repo_path(bsettings.backup_lxc_dir, project)
    if local:
        lxc_repo = str(copilot_repo)

    if source not in ("copilot", KIND_COPILOT, "") and source != "lxc":
        await log("Quelle SFTP — Repo nach Copilot holen …")
        await _ensure_sftp_repo_local(
            store,
            source=source,
            parent_id=parent_id,
            project=project,
            settings=settings,
            bsettings=bsettings,
        )

    if not local and (copilot_repo / "config").is_file():
        await log("Spiegele Copilot-Repo auf den Host für Restore …")
        await _mirror_copilot_to_lxc(
            settings,
            ip=ip,
            local=False,
            lxc_repo=lxc_repo,
            copilot_repo=copilot_repo,
            timeout=transfer_timeout,
            log=log,
        )

    await ensure_restic_installed(
        settings,
        ip=ip,
        local=local,
        allow_install=bsettings.restic_install,
        timeout=timeout,
        log=log,
        work_dir=f"{bsettings.backup_lxc_dir.rstrip('/')}/restic-work/{safe_name(project)}",
        install_timeout=bsettings.restic_install_timeout,
        transfer_timeout=transfer_timeout,
    )

    staging = f"{bsettings.backup_lxc_dir.rstrip('/')}/restore/{safe_name(project)}/{snap[:12]}"
    if local:
        staging = str(bsettings.copilot_dir / "_restore" / safe_name(project) / snap[:12])
    work = f"{staging}.work"
    pw_file = f"{work}/.restic-pass"
    if local:
        await sshutil.local_run_ok(f"mkdir -p -- {shlex.quote(work)} {shlex.quote(staging)}", timeout=30)
    else:
        assert ip is not None
        await sshutil.ensure_remote_dir(settings, ip, work, timeout=30)
        await sshutil.ensure_remote_dir(settings, ip, staging, timeout=30)

    await _write_password_file(
        settings, ip=ip, local=local, remote_path=pw_file, password=password, timeout=timeout
    )
    try:
        await log(f"restic restore {snap[:12]} → Staging …")
        script = _restore_apply_script(
            repo=lxc_repo if not local else str(copilot_repo),
            password_file=pw_file,
            snapshot_id=snap,
            staging=staging,
            inventory=inventory,
            progress_path=f"{work}/.hc_job_restic_restore.progress",
        )
        try:
            await sshutil.run_detached_and_poll(
                settings,
                ip,
                script,
                work_dir=work,
                job_name="restic_restore",
                local=local,
                overall_timeout=archive_timeout,
                poll_interval=5.0,
                short_timeout=min(60.0, timeout),
                log=log,
                progress_label="Restore",
            )
        except DockerControlError as exc:
            raise ResticError(translate_restic_error(exc.message)) from exc
        await log("restic check (Metadaten) …")
        check_cmd = (
            f"RESTIC_REPOSITORY={shlex.quote(lxc_repo if not local else str(copilot_repo))} "
            f"RESTIC_PASSWORD_FILE={shlex.quote(pw_file)} restic check"
        )
        try:
            if local:
                await sshutil.local_run_ok(check_cmd, timeout=min(600.0, archive_timeout))
            else:
                assert ip is not None
                await sshutil.ssh_run_ok(
                    settings, ip, check_cmd, timeout=min(600.0, archive_timeout)
                )
            await log("restic check OK")
        except DockerControlError as exc:
            await log(
                "Hinweis: restic check nach Restore: "
                + translate_restic_error(exc.message)
            )
    finally:
        await _remove_password_file(
            settings, ip=ip, local=local, remote_path=pw_file, timeout=timeout
        )


def _restore_apply_script(
    *,
    repo: str,
    password_file: str,
    snapshot_id: str,
    staging: str,
    inventory: dict[str, Any],
    progress_path: str,
) -> str:
    lines = [
        "set -euo pipefail",
        f"export RESTIC_REPOSITORY={shlex.quote(repo)}",
        f"export RESTIC_PASSWORD_FILE={shlex.quote(password_file)}",
        f"ST={shlex.quote(staging)}",
        f"PROG={shlex.quote(progress_path)}",
        _prog_line(progress_path, "restore"),
        'rm -rf "$ST"',
        'mkdir -p "$ST"',
        f"restic restore {shlex.quote(snapshot_id)} --target \"$ST\"",
        _prog_line(progress_path, "apply"),
    ]
    for vol in inventory.get("named_volumes") or []:
        name = vol.get("name") or ""
        mp = (vol.get("source") or "").strip()
        if not name:
            continue
        lines.append(
            f'MP={shlex.quote(mp)}\n'
            f'if [ -z "$MP" ] || [ ! -d "$ST$MP" ]; then\n'
            f'  MP=$(docker volume inspect -f "{{{{.Mountpoint}}}}" {shlex.quote(name)} 2>/dev/null || true)\n'
            f'fi\n'
            f'if [ -n "$MP" ] && [ -d "$ST$MP" ]; then\n'
            f'  mkdir -p -- "$MP"\n'
            f'  if command -v rsync >/dev/null 2>&1; then\n'
            f'    rsync -a --delete "$ST$MP"/ "$MP"/\n'
            f'  else\n'
            f'    rm -rf -- "$MP"/* "$MP"/.[!.]* "$MP"/..?* 2>/dev/null || true\n'
            f'    cp -a "$ST$MP"/. "$MP"/ 2>/dev/null || true\n'
            f'  fi\n'
            f'else\n'
            f'  docker volume create {shlex.quote(name)} >/dev/null 2>&1 || true\n'
            f'  docker run --rm -v {shlex.quote(name)}:/v -v "$ST":/in:ro alpine:3.20 '
            f'sh -c "rm -rf /v/..?* /v/.[!.]* /v/* 2>/dev/null; '
            f'if [ -d /in$MP ]; then cp -a /in$MP/. /v/; fi" 2>/dev/null || true\n'
            f'fi'
        )
    for bind in inventory.get("bind_mounts") or []:
        src = (bind.get("source") or "").strip()
        if not src:
            continue
        lines.append(
            f'if [ -d {shlex.quote(staging + src)} ] || [ -f {shlex.quote(staging + src)} ]; then\n'
            f'  mkdir -p -- {shlex.quote(src)}\n'
            f'  if command -v rsync >/dev/null 2>&1; then\n'
            f'    rsync -a --delete {shlex.quote(staging + src + "/")} {shlex.quote(src + "/")}\n'
            f'  else\n'
            f'    cp -a {shlex.quote(staging + src + "/.")} {shlex.quote(src + "/")} 2>/dev/null || '
            f'    cp -a {shlex.quote(staging + src)} {shlex.quote(src)} 2>/dev/null || true\n'
            f'  fi\n'
            f'fi'
        )
    wd = inventory.get("working_dir")
    if wd:
        for cf in inventory.get("compose_files") or []:
            lines.append(
                f'if [ -f {shlex.quote(staging + cf)} ]; then\n'
                f'  mkdir -p -- {shlex.quote(str(Path(cf).parent))}\n'
                f'  cp -a {shlex.quote(staging + cf)} {shlex.quote(cf)}\n'
                f'fi'
            )
        env = inventory.get("env_file")
        if env:
            lines.append(
                f'if [ -f {shlex.quote(staging + env)} ]; then '
                f'cp -a {shlex.quote(staging + env)} {shlex.quote(env)}; fi'
            )
        lines.append(
            f'if [ -d {shlex.quote(staging + wd)} ]; then\n'
            f'  mkdir -p -- {shlex.quote(wd)}\n'
            f'  cp -a {shlex.quote(staging + wd + "/.")} {shlex.quote(wd + "/")} 2>/dev/null || true\n'
            f'fi'
        )
    lines.append(_prog_line(progress_path, "done"))
    return "\n".join(lines)
