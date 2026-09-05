"""Restore a Compose stack from a Copilot or SFTP backup archive."""

from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.core.docker_control import DockerControlError, validate_docker_name
from app.core.locale import format_de, iso_utc, now_berlin
from app.core.models import TopologySnapshot

from backup_verifier.config import BackupSettings, get_backup_settings
from backup_verifier.destinations import (
    KIND_COPILOT,
    KIND_SFTP,
    ensure_seeded,
    resolve_auth,
)
from backup_verifier.inventory import build_inventory
from backup_verifier.restic import ENGINE_RESTIC, ResticError, run_restic_restore
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
    snapshot_id: str | None = None,
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
    engine = str(run.get("engine") or "tar").strip().lower()
    snap = (snapshot_id or run.get("snapshot_id") or "").strip()
    if engine == ENGINE_RESTIC:
        if not snap:
            raise RestoreError(
                "restic-Restore braucht eine Snapshot-ID (Lauf ohne Snapshot)."
            )
    elif not archive_name or not archive_sha:
        raise RestoreError("Backup-Metadaten unvollständig (Archiv/Checksum fehlen).")

    restore_id = await store.create_restore(backup_run_id=run_id, source=source)

    async def log(msg: str) -> None:
        logger.info("[restore %s] %s", restore_id, msg)
        await store.append_restore_log(
            restore_id, f"{format_de(now_berlin())} · {msg}"
        )

    try:
        if engine == ENGINE_RESTIC:
            inventory = await build_inventory(
                settings,
                parent_id=parent_id,
                project=project,
                snapshot=snapshot,
                lxc_backup_dir=bsettings.backup_lxc_dir,
            )
            await log(f"restic-Restore Snapshot {snap[:12]}…")
            # Stop stack
            await log("Stoppe Stack vor Restore …")
            wd = inventory.get("working_dir")
            is_local = bool(inventory["local"])
            ip = inventory["host_ip"]
            timeout = bsettings.backup_ssh_timeout
            if wd:
                stop_cmd = f"cd {shlex.quote(wd)} && docker compose stop"
            else:
                stop_cmd = f"docker compose -p {shlex.quote(project)} stop"
            if is_local:
                await sshutil.local_run_ok(stop_cmd, timeout=timeout)
            else:
                assert ip is not None
                await sshutil.ssh_run_ok(settings, ip, stop_cmd, timeout=timeout)
            restic_up = False
            try:
                await run_restic_restore(
                    store,
                    restore_id=restore_id,
                    parent_id=parent_id,
                    project=project,
                    snapshot_id=snap,
                    source=source,
                    inventory=inventory,
                    settings=settings,
                    bsettings=bsettings,
                    log=log,
                )
            except ResticError as exc:
                raise RestoreError(exc.message) from exc
            finally:
                if not restic_up:
                    try:
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
                        restic_up = True
                    except Exception:
                        logger.exception("Stack nach restic-Restore nicht gestartet")
            if not restic_up:
                raise RestoreError("Stack nach Restore nicht gestartet.")
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
                "snapshot_id": snap,
                "status": "success",
                "message": f"Stack „{project}“ aus restic-Snapshot {snap[:12]} wiederhergestellt.",
            }

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
    hops = run.get("destinations") if isinstance(run.get("destinations"), list) else []

    await ensure_seeded(store)
    dest_rows = await store.list_destinations()

    dest: dict[str, Any] | None = None
    if str(source).isdigit():
        dest = await store.get_destination(int(source))
    elif source in ("copilot", KIND_COPILOT):
        dest = next((d for d in dest_rows if d.get("kind") == KIND_COPILOT), None)
    elif source in ("synology",):
        dest = next(
            (
                d
                for d in dest_rows
                if d.get("kind") == KIND_SFTP and d.get("preset") == "synology"
            ),
            None,
        )
        if dest is None:
            dest = next((d for d in dest_rows if d.get("kind") == KIND_SFTP), None)
    else:
        dest = next(
            (
                d
                for d in dest_rows
                if str(d.get("kind")) == source or str(d.get("label")) == source
            ),
            None,
        )

    hop_match = None
    if dest and hops:
        hop_match = next(
            (
                h
                for h in hops
                if h.get("id") == dest.get("id")
                or (
                    h.get("kind") == dest.get("kind")
                    and h.get("status") in ("ok", "cleared")
                )
            ),
            None,
        )
    elif hops and source in ("copilot", KIND_COPILOT):
        hop_match = next((h for h in hops if h.get("kind") == KIND_COPILOT), None)

    if dest and dest.get("kind") == KIND_SFTP:
        syn_path = (hop_match or {}).get("path") or ""
        if not syn_path:
            remote_base = (dest.get("remote_path") or "").rstrip("/")
            syn_path = f"{remote_base}/{project}/{archive_name}"
        dest_file = bsettings.copilot_dir / project / archive_name
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        await log(f"Hole Archiv von {dest.get('label')}: {syn_path}")
        auth = resolve_auth(dest, settings)
        await sshutil.scp_get(
            settings,
            dest["host"],
            syn_path,
            dest_file,
            timeout=bsettings.backup_transfer_timeout,
            username=auth["username"],
            key=auth.get("key"),
            key_pem=auth.get("key_pem"),
            password=auth.get("password"),
            port=auth["port"],
        )
        return dest_file

    path_str = (hop_match or {}).get("path") or run.get("copilot_path")
    if path_str and Path(path_str).is_file():
        return Path(path_str)
    candidate = bsettings.copilot_dir / project / archive_name
    if candidate.is_file():
        return candidate

    for h in hops:
        if h.get("kind") == KIND_SFTP and h.get("status") == "ok" and h.get("path"):
            await log("Copilot-Kopie fehlt — Fallback SFTP-Hop")
            sid = h.get("id")
            return await _resolve_archive(
                store,
                run=run,
                source=str(sid) if sid is not None else "synology",
                bsettings=bsettings,
                settings=settings,
                log=log,
            )

    raise RestoreError(
        f"Archiv nicht gefunden unter Copilot ({candidate}). "
        "SFTP-Fallback nicht möglich."
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
