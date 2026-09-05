"""Restore files or a Compose stack to staging (default) or original paths."""

from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import Any, Awaitable, Callable

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
from backup_verifier.restore_paths import (
    DEST_ORIGINAL,
    DEST_STAGING,
    PLACE_COPILOT,
    PLACE_GUEST,
    RestorePlanError,
    copilot_staging_dir,
    describe_restore,
    guest_staging_dir,
    jail_restore_paths,
    normalize_dest_mode,
    normalize_dest_place,
    normalize_scope,
    staging_stamp,
    validate_restore_confirm,
)
from backup_verifier import sshutil
from backup_verifier.store import BackupStore
from backup_verifier.verify import verify_local_file

logger = logging.getLogger(__name__)

LogFn = Callable[[str], Awaitable[None]]
ProgressFn = Callable[..., Awaitable[None]]


class RestoreError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def _noop_progress(**_kwargs: Any) -> None:
    return None


async def run_restore(
    store: BackupStore,
    *,
    run_id: int,
    snapshot: TopologySnapshot | None,
    confirm: bool,
    source: str = "copilot",
    snapshot_id: str | None = None,
    dest_mode: str = DEST_STAGING,
    dest_place: str = PLACE_COPILOT,
    scope: str = "stack",
    paths: list[str] | None = None,
    typed_confirm: str | None = None,
    settings: Settings | None = None,
    bsettings: BackupSettings | None = None,
    on_progress: ProgressFn | None = None,
    on_log: LogFn | None = None,
) -> dict[str, Any]:
    """Restore after explicit confirm. Default destination is staging, not live binds."""
    settings = settings or get_settings()
    bsettings = bsettings or get_backup_settings()
    progress = on_progress or _noop_progress

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

    try:
        mode = normalize_dest_mode(dest_mode)
        place = normalize_dest_place(dest_place, dest_mode=mode)
        sc = normalize_scope(scope, paths)
        jailed = jail_restore_paths(paths, scope=sc)
        validate_restore_confirm(
            confirm=confirm, dest_mode=mode, typed_confirm=typed_confirm, stack=project
        )
    except RestorePlanError as exc:
        raise RestoreError(exc.message) from exc

    if engine == ENGINE_RESTIC:
        if not snap:
            raise RestoreError(
                "restic-Restore braucht eine Snapshot-ID (Lauf ohne Snapshot)."
            )
    elif not archive_name or not archive_sha:
        raise RestoreError("Backup-Metadaten unvollständig (Archiv/Checksum fehlen).")

    stamp = staging_stamp()
    if place == PLACE_COPILOT:
        staging = str(copilot_staging_dir(bsettings.copilot_dir, project, stamp))
    else:
        staging = guest_staging_dir(bsettings.backup_lxc_dir, project, stamp)

    restore_id = await store.create_restore(
        backup_run_id=run_id,
        source=source,
        dest_mode=mode,
        dest_place=place,
        scope=sc,
        paths=jailed,
        staging_path=staging,
    )

    async def log(msg: str) -> None:
        logger.info("[restore %s] %s", restore_id, msg)
        await store.append_restore_log(
            restore_id, f"{format_de(now_berlin())} · {msg}"
        )
        if on_log:
            await on_log(msg)

    plan = describe_restore(
        stack=project,
        source_label=source,
        snapshot_or_archive=snap or archive_name,
        dest_mode=mode,
        dest_place=place,
        scope=sc,
        paths=jailed,
        staging_path=staging,
    )
    await log(plan["summary"])
    await progress(phase="Vorbereitung", percent=4, message=plan["dest_label"])

    try:
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
        apply_original = mode == DEST_ORIGINAL
        stop_stack = apply_original

        if engine == ENGINE_RESTIC:
            await progress(
                phase="restic", percent=12, message="restic-Restore nach Staging …"
            )
            if stop_stack:
                await log("Stoppe Stack vor Original-Restore …")
                await _stack_stop(settings, inventory, timeout=timeout)
            restic_up = False
            try:
                staging_out = await run_restic_restore(
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
                    apply_original=apply_original,
                    include_paths=jailed,
                    staging_override=staging,
                    dest_place=place,
                )
                staging = staging_out or staging
            except ResticError as exc:
                raise RestoreError(exc.message) from exc
            finally:
                if stop_stack and not restic_up:
                    try:
                        await log("Starte Stack (docker compose up -d) …")
                        await _stack_up(settings, inventory, timeout=timeout)
                        restic_up = True
                    except Exception:
                        logger.exception("Stack nach restic-Restore nicht gestartet")
            if stop_stack and not restic_up:
                raise RestoreError("Stack nach Restore nicht gestartet.")
            return await _finish_ok(
                store,
                restore_id=restore_id,
                run_id=run_id,
                project=project,
                snap=snap,
                mode=mode,
                place=place,
                staging=staging,
                log=log,
            )

        await progress(phase="Archiv", percent=15, message="Archiv holen …")
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

        if stop_stack:
            await log("Stoppe Stack vor Original-Restore …")
            await progress(phase="Stop", percent=25, message="Stoppe Stack …")
            await _stack_stop(settings, inventory, timeout=timeout)

        extract_on_guest = place == PLACE_GUEST and not is_local
        if extract_on_guest:
            remote_dir = staging
            remote_archive = f"{remote_dir}/{archive_name}"
            await log(f"Übertrage Archiv nach {remote_archive}")
            await progress(phase="Transfer", percent=35, message="Archiv zum Gast …")
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
                apply_original=apply_original,
                include_paths=jailed,
            )
            await log("Extrahiere nach Staging …")
            await progress(phase="Extract", percent=55, message="tar-Extract …")
            await sshutil.run_detached_and_poll(
                settings,
                ip,
                script,
                work_dir=remote_dir,
                job_name="restore",
                local=False,
                overall_timeout=archive_timeout,
                poll_interval=5.0,
                short_timeout=min(60.0, timeout),
                log=log,
            )
        else:
            Path(staging).mkdir(parents=True, exist_ok=True)
            extract = str(Path(staging) / "extract")
            await log(f"Extrahiere lokal nach {extract}")
            await progress(phase="Extract", percent=50, message="tar-Extract (Copilot) …")
            script = _restore_script(
                remote_archive=str(local_archive),
                extract_dir=extract,
                inventory=inventory,
                apply_original=False,
                include_paths=jailed,
            )
            await sshutil.run_detached_and_poll(
                settings,
                ip,
                script,
                work_dir=staging,
                job_name="restore",
                local=True,
                overall_timeout=archive_timeout,
                poll_interval=5.0,
                short_timeout=min(60.0, timeout),
                log=log,
            )
            if apply_original and not is_local:
                await log("Kopiere Staging auf den Gast (Originalpfad) …")
                await progress(
                    phase="Transfer", percent=70, message="Staging → Gast …"
                )
                assert ip is not None
                guest_dir = guest_staging_dir(
                    bsettings.backup_lxc_dir, project, stamp + "-orig"
                )
                await sshutil.ensure_remote_dir(settings, ip, guest_dir, timeout=30)
                pushed = await sshutil.rsync_push(
                    settings,
                    ip,
                    Path(extract),
                    f"{guest_dir}/extract",
                    timeout=transfer_timeout,
                    log=log,
                )
                if not pushed:
                    raise RestoreError(
                        "rsync zum Gast fehlgeschlagen. "
                        "Original-Restore bitte mit Ziel „Gast“ starten "
                        "(Archiv wird per SCP übertragen)."
                    )
                apply_script = _restore_script(
                    remote_archive=str(local_archive),
                    extract_dir=f"{guest_dir}/extract",
                    inventory=inventory,
                    apply_original=True,
                    include_paths=jailed,
                    skip_extract=True,
                )
                await sshutil.run_detached_and_poll(
                    settings,
                    ip,
                    apply_script,
                    work_dir=guest_dir,
                    job_name="restore_apply",
                    local=False,
                    overall_timeout=archive_timeout,
                    poll_interval=5.0,
                    short_timeout=min(60.0, timeout),
                    log=log,
                )
            elif apply_original and is_local:
                apply_script = _restore_script(
                    remote_archive=str(local_archive),
                    extract_dir=extract,
                    inventory=inventory,
                    apply_original=True,
                    include_paths=jailed,
                    skip_extract=True,
                )
                await sshutil.run_detached_and_poll(
                    settings,
                    ip,
                    apply_script,
                    work_dir=staging,
                    job_name="restore_apply",
                    local=True,
                    overall_timeout=archive_timeout,
                    poll_interval=5.0,
                    short_timeout=min(60.0, timeout),
                    log=log,
                )

        if stop_stack:
            await log("Starte Stack (docker compose up -d) …")
            await progress(phase="Start", percent=90, message="Stack starten …")
            await _stack_up(settings, inventory, timeout=timeout)

        return await _finish_ok(
            store,
            restore_id=restore_id,
            run_id=run_id,
            project=project,
            snap=snap,
            mode=mode,
            place=place,
            staging=staging,
            log=log,
        )
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


async def _finish_ok(
    store: BackupStore,
    *,
    restore_id: int,
    run_id: int,
    project: str,
    snap: str,
    mode: str,
    place: str,
    staging: str,
    log: LogFn,
) -> dict[str, Any]:
    now = now_berlin()
    await store.update_restore(
        restore_id,
        status="success",
        finished_at=format_de(now),
        finished_at_iso=iso_utc(now),
        staging_path=staging,
    )
    if mode == DEST_STAGING:
        dest = "Copilot-Staging" if place == PLACE_COPILOT else "Gast-Staging"
        message = (
            f"Stack „{project}“ nach {dest} wiederhergestellt: {staging}. "
            "Live-Binds wurden nicht überschrieben."
        )
    else:
        message = f"Stack „{project}“ an Originalpfade wiederhergestellt."
    await log(message)
    return {
        "ok": True,
        "restore_id": restore_id,
        "backup_run_id": run_id,
        "snapshot_id": snap or None,
        "status": "success",
        "dest_mode": mode,
        "dest_place": place,
        "staging_path": staging,
        "message": message,
    }


async def _stack_stop(
    settings: Settings, inventory: dict[str, Any], *, timeout: float
) -> None:
    wd = inventory.get("working_dir")
    project = inventory.get("stack") or inventory.get("project") or ""
    if wd:
        stop_cmd = f"cd {shlex.quote(wd)} && docker compose stop"
    else:
        stop_cmd = f"docker compose -p {shlex.quote(project)} stop"
    if inventory.get("local"):
        await sshutil.local_run_ok(stop_cmd, timeout=timeout)
    else:
        ip = inventory["host_ip"]
        assert ip is not None
        await sshutil.ssh_run_ok(settings, ip, stop_cmd, timeout=timeout)


async def _stack_up(
    settings: Settings, inventory: dict[str, Any], *, timeout: float
) -> None:
    wd = inventory.get("working_dir")
    project = inventory.get("stack") or inventory.get("project") or ""
    if wd:
        up_cmd = f"cd {shlex.quote(wd)} && docker compose up -d"
    else:
        up_cmd = f"docker compose -p {shlex.quote(project)} up -d"
    if inventory.get("local"):
        await sshutil.local_run_ok(up_cmd, timeout=timeout)
    else:
        ip = inventory["host_ip"]
        assert ip is not None
        await sshutil.ssh_run_ok(settings, ip, up_cmd, timeout=timeout)


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
    apply_original: bool = False,
    include_paths: list[str] | None = None,
    skip_extract: bool = False,
) -> str:
    lines = [
        "set -euo pipefail",
        f"ARCH={shlex.quote(remote_archive)}",
        f"EX={shlex.quote(extract_dir)}",
    ]
    if not skip_extract:
        lines.extend(
            [
                'rm -rf "$EX"',
                'mkdir -p "$EX"',
            ]
        )
        members = ""
        for rel in include_paths or []:
            rel = (rel or "").strip().lstrip("/")
            if rel:
                members += f" {shlex.quote(rel)}"
        if members:
            lines.append(f'tar -xzf "$ARCH" -C "$EX"{members}')
        else:
            lines.append('tar -xzf "$ARCH" -C "$EX"')
    if not apply_original:
        return "\n".join(lines)

    import re as _re

    for vol in inventory.get("named_volumes") or []:
        name = vol["name"]
        safe_file = _re.sub(r"[^a-zA-Z0-9_.-]+", "_", name)
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

    for bind in inventory.get("bind_mounts") or []:
        src = bind["source"]
        safe = _re.sub(r"[^a-zA-Z0-9_.-]+", "_", src.strip("/"))[:80] or "bind"
        lines.append(
            f'if [ -f "$EX/binds/{safe}.tar.gz" ]; then\n'
            f'  mkdir -p -- {shlex.quote(src)}\n'
            f'  if [ -d {shlex.quote(src)} ]; then\n'
            f'    tar xzf "$EX/binds/{safe}.tar.gz" -C {shlex.quote(src)}\n'
            f'  fi\n'
            f'fi'
        )

    wd = inventory.get("working_dir")
    if wd:
        lines.append(
            f'if [ -d "$EX/compose" ]; then\n'
            f'  mkdir -p -- {shlex.quote(wd)}\n'
            f'  cp -a "$EX/compose/." {shlex.quote(wd)}/ 2>/dev/null || true\n'
            f'fi'
        )
    return "\n".join(lines)


def list_tar_members(archive: Path, *, limit: int = 400) -> list[dict[str, Any]]:
    """Cheap local ``tar -tzf`` — no extract. Used for path picker + drills."""
    import tarfile

    if not archive.is_file():
        raise RestoreError(f"Archiv nicht gefunden: {archive.name}")
    names: list[dict[str, Any]] = []
    try:
        with tarfile.open(archive, "r:*") as tf:
            for i, info in enumerate(tf):
                if i >= limit:
                    break
                name = (info.name or "").lstrip("./")
                if not name or name.startswith("../"):
                    continue
                names.append(
                    {
                        "path": name,
                        "type": "dir" if info.isdir() else "file",
                        "size": int(info.size or 0),
                    }
                )
    except (tarfile.TarError, OSError) as exc:
        raise RestoreError(f"Archiv nicht listbar: {exc}") from exc
    return names
