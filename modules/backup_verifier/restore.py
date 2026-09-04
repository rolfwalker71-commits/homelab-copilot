"""Restore a Compose stack from a Copilot (or Synology) backup archive."""

from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.core.docker_control import DockerControlError, resolve_parent_ip, validate_docker_name
from app.core.locale import format_de, iso_utc, now_berlin
from app.core.models import TopologySnapshot

from backup_verifier.config import BackupSettings, get_backup_settings
from backup_verifier.inventory import build_inventory
from backup_verifier import sshutil
from backup_verifier.store import BackupStore
from backup_verifier.verify import verify_local_file

logger = logging.getLogger(__name__)


class RestoreError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def run_restore(
    store: BackupStore,
    *,
    run_id: int,
    snapshot: TopologySnapshot | None,
    confirm: bool,
    source: str = "copilot",
    settings: Settings | None = None,
    bsettings: BackupSettings | None = None,
) -> dict[str, Any]:
    if not confirm:
        raise RestoreError(
            "Wiederherstellung erfordert Bestätigung (confirm=true). "
            "Der Stack wird gestoppt und Volumes überschrieben."
        )
    settings = settings or get_settings()
    bsettings = bsettings or get_backup_settings()
    run = await store.get_run(run_id)
    if not run:
        raise RestoreError(f"Backup-Lauf #{run_id} nicht gefunden.")
    if run.get("status") not in ("success", "partial"):
        raise RestoreError(
            f"Backup #{run_id} hat Status „{run.get('status')}“ — Restore nicht sinnvoll."
        )

    project = validate_docker_name(run["stack"], kind="Compose-Projekt")
    parent_id = run["parent_id"]
    archive_sha = (run.get("archive_sha256") or "").lower()
    archive_name = run.get("archive_name") or ""
    if not archive_name or not archive_sha:
        raise RestoreError("Backup-Metadaten unvollständig (Archiv/Checksum fehlen).")

    restore_id = await store.create_restore(backup_run_id=run_id, source=source)

    async def log(msg: str) -> None:
        logger.info("[restore %s] %s", restore_id, msg)
        await store.append_restore_log(
            restore_id, f"{format_de(now_berlin())} · {msg}"
        )

    try:
        local_archive = await _resolve_archive(
            store,
            run=run,
            source=source,
            bsettings=bsettings,
            settings=settings,
            log=log,
        )
        ok, msg = verify_local_file(local_archive, archive_sha)
        if not ok:
            raise RestoreError(f"Archiv-Verify vor Restore fehlgeschlagen: {msg}")
        await log(f"Archiv verifiziert: {local_archive.name}")

        inventory = await build_inventory(
            settings,
            parent_id=parent_id,
            project=project,
            snapshot=snapshot,
            lxc_backup_dir=bsettings.backup_lxc_dir,
        )
        ip = inventory["host_ip"]
        is_local = bool(inventory["local"])
        timeout = bsettings.backup_ssh_timeout
        archive_timeout = bsettings.backup_archive_timeout
        transfer_timeout = bsettings.backup_transfer_timeout

        # Stop stack
        await log("Stoppe Stack vor Restore …")
        wd = inventory.get("working_dir")
        if wd:
            stop_cmd = f"cd {shlex.quote(wd)} && docker compose stop"
        else:
            stop_cmd = f"docker compose -p {shlex.quote(project)} stop"
        if is_local:
            await sshutil.local_run_ok(stop_cmd, timeout=timeout)
        else:
            assert ip is not None
            await sshutil.ssh_run_ok(settings, ip, stop_cmd, timeout=timeout)

        # Upload + extract on target
        remote_dir = f"{bsettings.backup_lxc_dir.rstrip('/')}/restore/{project}"
        remote_archive = f"{remote_dir}/{archive_name}"
        await log(f"Übertrage Archiv nach {remote_archive}")
        if is_local:
            await sshutil.local_run_ok(
                f"mkdir -p -- {shlex.quote(remote_dir)} && "
                f"cp -f -- {shlex.quote(str(local_archive))} {shlex.quote(remote_archive)}",
                timeout=transfer_timeout,
            )
        else:
            assert ip is not None
            await sshutil.ensure_remote_dir(settings, ip, remote_dir, timeout=30)
            await sshutil.scp_put(
                settings, ip, local_archive, remote_archive, timeout=transfer_timeout
            )

        extract = f"{remote_dir}/extract"
        script = _restore_script(
            remote_archive=remote_archive,
            extract_dir=extract,
            inventory=inventory,
        )
        await log("Extrahiere und stelle Volumes/Binds wieder her …")
        await sshutil.run_detached_and_poll(
            settings,
            ip,
            script,
            work_dir=remote_dir,
            job_name="restore",
            local=is_local,
            overall_timeout=archive_timeout,
            poll_interval=5.0,
            short_timeout=min(60.0, timeout),
            log=log,
        )

        # compose up
        await log("Starte Stack (docker compose up -d) …")
        if wd:
            up_cmd = f"cd {shlex.quote(wd)} && docker compose up -d"
        else:
            up_cmd = f"docker compose -p {shlex.quote(project)} up -d"
        if is_local:
            await sshutil.local_run_ok(up_cmd, timeout=timeout)
        else:
            assert ip is not None
            await sshutil.ssh_run_ok(settings, ip, up_cmd, timeout=timeout)

        now = now_berlin()
        await store.update_restore(
            restore_id,
            status="success",
            finished_at=format_de(now),
            finished_at_iso=iso_utc(now),
        )
        await log("Restore erfolgreich")
        return {
            "ok": True,
            "restore_id": restore_id,
            "backup_run_id": run_id,
            "status": "success",
            "message": f"Stack „{project}“ wiederhergestellt.",
        }
    except Exception as exc:
        msg = getattr(exc, "message", None) or str(exc)
        await log(f"Fehler: {msg}")
        now = now_berlin()
        await store.update_restore(
            restore_id,
            status="failed",
            finished_at=format_de(now),
            finished_at_iso=iso_utc(now),
            error_message=msg,
        )
        if isinstance(exc, (RestoreError, DockerControlError)):
            raise
        raise RestoreError(msg) from exc


async def _resolve_archive(
    store: BackupStore,
    *,
    run: dict[str, Any],
    source: str,
    bsettings: BackupSettings,
    settings: Settings,
    log,
) -> Path:
    archive_name = run["archive_name"]
    project = run["stack"]
    expected = (run.get("archive_sha256") or "").lower()

    if source == "synology":
        if not bsettings.synology_configured:
            raise RestoreError("Synology nicht konfiguriert.")
        syn_path = run.get("synology_path") or (
            f"{bsettings.backup_synology_path.rstrip('/')}/{project}/{archive_name}"
        )
        dest = bsettings.copilot_dir / project / archive_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        await log(f"Hole Archiv von Synology: {syn_path}")
        await sshutil.scp_get(
            settings,
            bsettings.backup_synology_host,
            syn_path,
            dest,
            timeout=bsettings.backup_transfer_timeout,
            username=bsettings.backup_synology_user,
            key=bsettings.synology_key(),
            port=bsettings.backup_synology_port,
        )
        return dest

    # Default: copilot
    path_str = run.get("copilot_path")
    if path_str and Path(path_str).is_file():
        return Path(path_str)
    candidate = bsettings.copilot_dir / project / archive_name
    if candidate.is_file():
        return candidate

    # Fallback: pull from Synology if Copilot copy missing
    if bsettings.synology_configured and run.get("synology_status") == "ok":
        await log("Copilot-Kopie fehlt — fallback Synology")
        return await _resolve_archive(
            store,
            run=run,
            source="synology",
            bsettings=bsettings,
            settings=settings,
            log=log,
        )
    raise RestoreError(
        f"Archiv nicht gefunden unter Copilot ({candidate}). "
        "Synology-Fallback nicht möglich."
    )


def _restore_script(
    *,
    remote_archive: str,
    extract_dir: str,
    inventory: dict[str, Any],
) -> str:
    lines = [
        "set -euo pipefail",
        f"ARCH={shlex.quote(remote_archive)}",
        f"EX={shlex.quote(extract_dir)}",
        'rm -rf "$EX"',
        'mkdir -p "$EX"',
        'tar -xzf "$ARCH" -C "$EX"',
    ]
    # Restore named volumes
    for vol in inventory.get("named_volumes") or []:
        name = vol["name"]
        safe = name  # file was named with sanitized form — try both
        import re

        safe_file = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name)
        lines.append(
            f'VOLFILE=""\n'
            f'if [ -f "$EX/volumes/{safe_file}.tar.gz" ]; then VOLFILE="$EX/volumes/{safe_file}.tar.gz"; fi\n'
            f'if [ -n "$VOLFILE" ]; then\n'
            f'  docker volume create {shlex.quote(name)} >/dev/null 2>&1 || true\n'
            f'  docker run --rm -v {shlex.quote(name)}:/v -v "$(dirname "$VOLFILE"):/in:ro" '
            f'alpine:3.20 sh -c "rm -rf /v/..?* /v/.[!.]* /v/* 2>/dev/null; '
            f'tar xzf /in/$(basename "$VOLFILE") -C /v" '
            f'|| docker run --rm -v {shlex.quote(name)}:/v -v "$(dirname "$VOLFILE"):/in:ro" '
            f'busybox:1.36 sh -c "rm -rf /v/*; tar xzf /in/$(basename "$VOLFILE") -C /v"\n'
            f'fi'
        )

    # Restore bind mounts (overwrite carefully)
    for bind in inventory.get("bind_mounts") or []:
        if not bind.get("readable") and bind.get("readable") is not False:
            pass
        src = bind["source"]
        import re

        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", src.strip("/"))[:80] or "bind"
        lines.append(
            f'if [ -f "$EX/binds/{safe}.tar.gz" ]; then\n'
            f'  mkdir -p -- {shlex.quote(src)}\n'
            f'  if [ -d {shlex.quote(src)} ]; then\n'
            f'    tar xzf "$EX/binds/{safe}.tar.gz" -C {shlex.quote(src)}\n'
            f'  fi\n'
            f'fi'
        )

    # Restore compose files if working_dir known
    wd = inventory.get("working_dir")
    if wd:
        lines.append(
            f'if [ -d "$EX/compose" ]; then\n'
            f'  mkdir -p -- {shlex.quote(wd)}\n'
            f'  cp -a "$EX/compose/." {shlex.quote(wd)}/ 2>/dev/null || true\n'
            f'fi'
        )
    return "\n".join(lines)
