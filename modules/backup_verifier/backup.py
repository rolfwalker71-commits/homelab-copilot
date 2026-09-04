"""Compose-stack backup: host staging → ordered durable destinations."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Awaitable

from app.config import Settings, get_settings
from app.core.docker_control import DockerControlError, validate_docker_name
from app.core.locale import format_de, iso_utc, now_berlin
from app.core.models import TopologySnapshot

from backup_verifier.config import BackupSettings, get_backup_settings
from backup_verifier.destinations import (
    KIND_COPILOT,
    KIND_HOST,
    KIND_SFTP,
    get_pipeline,
    legacy_role_for,
    resolve_auth,
)
from backup_verifier.inventory import build_inventory
from backup_verifier import sshutil
from backup_verifier.store import BackupStore
from backup_verifier.verify import (
    summarize_hop_verifies,
    validate_manifest,
    verify_local_file,
)

logger = logging.getLogger(__name__)

LogFn = Callable[[str], Awaitable[None]]
ProgressFn = Callable[..., Awaitable[None]]

_SAFE_ARCHIVE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


class BackupError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# Per-stack lock to avoid concurrent backups of the same project
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(parent_id: str, project: str) -> asyncio.Lock:
    key = f"{parent_id}::{project}"
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


def _stamp() -> str:
    return now_berlin().strftime("%Y%m%d_%H%M%S")


def _archive_basename(project: str, stamp: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", project)
    return f"{safe}_{stamp}.tar.gz"


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


async def _apply_legacy_fields(
    store: BackupStore,
    run_id: int,
    hop: dict[str, Any],
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


async def _purge_project_staging(
    settings: Settings,
    *,
    ip: str | None,
    local: bool,
    project_dir: str,
    timeout: float,
    log: LogFn,
) -> None:
    """Remove leftover archives/work under the project staging dir before a new run."""
    script = f"""
set -e
DIR={shlex.quote(project_dir)}
[ -d "$DIR" ] || exit 0
rm -rf -- "$DIR"/*
"""
    try:
        if local:
            await sshutil.local_run(script, timeout=timeout)
        else:
            if not ip:
                return
            await sshutil.ssh_run(settings, ip, script, timeout=timeout)
        await log(f"Host-Staging bereinigt: {project_dir}")
    except Exception as exc:
        await log(f"Hinweis: Staging-Cleanup übersprungen ({exc})")


async def _delete_paths(
    settings: Settings,
    *,
    ip: str | None,
    local: bool,
    paths: list[str],
    timeout: float,
) -> None:
    if not paths:
        return
    quoted = " ".join(shlex.quote(p) for p in paths)
    cmd = f"rm -rf -- {quoted}"
    if local:
        await sshutil.local_run(cmd, timeout=timeout)
    else:
        assert ip is not None
        await sshutil.ssh_run(settings, ip, cmd, timeout=timeout)


async def run_backup(
    store: BackupStore,
    *,
    parent_id: str,
    project: str,
    snapshot: TopologySnapshot | None,
    settings: Settings | None = None,
    bsettings: BackupSettings | None = None,
    quiesce: bool | None = None,
    on_progress: ProgressFn | None = None,
    on_log: LogFn | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    bsettings = bsettings or get_backup_settings()
    project = validate_docker_name(project, kind="Compose-Projekt")
    lock = _lock_for(parent_id, project)

    if lock.locked():
        raise BackupError(
            f"Backup für „{project}“ läuft bereits — bitte warten."
        )

    async with lock:
        return await _run_backup_locked(
            store,
            parent_id=parent_id,
            project=project,
            snapshot=snapshot,
            settings=settings,
            bsettings=bsettings,
            quiesce=bsettings.backup_quiesce if quiesce is None else quiesce,
            on_progress=on_progress,
            on_log=on_log,
        )


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
        # Allow simpler callbacks without run_id kwarg
        await on_progress(phase=phase, percent=percent, message=message)


async def _run_backup_locked(
    store: BackupStore,
    *,
    parent_id: str,
    project: str,
    snapshot: TopologySnapshot | None,
    settings: Settings,
    bsettings: BackupSettings,
    quiesce: bool,
    on_progress: ProgressFn | None = None,
    on_log: LogFn | None = None,
) -> dict[str, Any]:
    await _emit_progress(
        on_progress,
        phase="Preflight",
        percent=5,
        message="Inventar wird ermittelt…",
    )
    inventory = await build_inventory(
        settings,
        parent_id=parent_id,
        project=project,
        snapshot=snapshot,
        lxc_backup_dir=bsettings.backup_lxc_dir,
    )
    run_id = await store.create_run(
        stack=project,
        parent_id=parent_id,
        guest_name=inventory["guest_name"],
        preflight=inventory,
    )
    await _emit_progress(
        on_progress,
        phase="Preflight",
        percent=8,
        message=f"Preflight: Stack „{project}“",
        run_id=run_id,
    )

    async def log(msg: str) -> None:
        logger.info("[backup %s] %s", run_id, msg)
        line = f"{format_de(now_berlin())} · {msg}"
        await store.append_log(run_id, line)
        if on_log:
            try:
                await on_log(line)
            except Exception:
                logger.debug("on_log callback failed", exc_info=True)

    await log(f"Preflight: Stack „{project}“ auf {inventory['guest_name']}")
    await log(
        f"Enthalten: {inventory['include_summary']['compose_files']} Compose-Datei(en), "
        f"{inventory['include_summary']['named_volumes']} Volume(s), "
        f"{inventory['include_summary']['bind_mounts_readable']} Bind(s)"
    )
    for g in inventory.get("gaps") or []:
        await log(f"Hinweis: {g}")

    stamp = _stamp()
    archive_name = _archive_basename(project, stamp)
    work_rel = f"{project}/{stamp}"
    local = bool(inventory["local"])
    ip = inventory["host_ip"]
    # Local socket backups stage under Copilot data dir (writable in container)
    if local:
        base = str(bsettings.copilot_dir / "_staging")
    else:
        base = bsettings.backup_lxc_dir.rstrip("/")
    remote_work = f"{base}/{work_rel}"
    remote_archive = f"{base}/{project}/{archive_name}"
    project_staging = f"{base}/{project}"
    timeout = bsettings.backup_ssh_timeout
    archive_timeout = bsettings.backup_archive_timeout
    transfer_timeout = bsettings.backup_transfer_timeout
    started = False
    stopped = False

    pipeline = await get_pipeline(store)
    durable = [d for d in pipeline if d.get("kind") != KIND_HOST]
    if not durable:
        raise BackupError("Keine dauerhaften Backup-Ziele aktiv (Copilot/SFTP).")

    hop_results: list[dict[str, Any]] = []
    archive_sha = ""
    size_bytes = 0
    copilot_path: Path | None = None
    local_source: Path | None = None  # durable buffer for subsequent SFTP hops
    manifest: dict[str, Any] = {}
    host_purged = False

    try:
        await _purge_project_staging(
            settings,
            ip=ip,
            local=local,
            project_dir=project_staging,
            timeout=timeout,
            log=log,
        )

        # --- Quiesce ---
        if quiesce:
            await _emit_progress(
                on_progress,
                phase="Quiesce",
                percent=12,
                message="Stack wird gestoppt (Quiesce)…",
                run_id=run_id,
            )
            await log("Quiesce: docker compose stop …")
            await _compose_stop(settings, ip, project, inventory, local=local, timeout=timeout)
            stopped = True
        else:
            await log("Quiesce deaktiviert — Volumes werden live gesichert")

        # --- Build archive on host staging ---
        host_dest = next((d for d in pipeline if d.get("kind") == KIND_HOST), None)
        await _emit_progress(
            on_progress,
            phase="Config",
            percent=20,
            message="Compose & Konfiguration sichern…",
            run_id=run_id,
        )
        await log(f"Erstelle Archiv unter {remote_work}")
        await _emit_progress(
            on_progress,
            phase="Volumes",
            percent=30,
            message="Volumes & Bind-Mounts werden gesichert (kann dauern)…",
            run_id=run_id,
        )
        manifest, archive_sha, size_bytes = await _create_archive(
            settings,
            inventory=inventory,
            remote_work=remote_work,
            remote_archive=remote_archive,
            local=local,
            ip=ip,
            quiesced=quiesce,
            timeout=timeout,
            archive_timeout=archive_timeout,
            log=log,
            on_progress=on_progress,
            run_id=run_id,
        )
        await _emit_progress(
            on_progress,
            phase="Host-Staging",
            percent=55,
            message="Archiv erstellt — Staging prüfen…",
            run_id=run_id,
        )
        await store.update_run(
            run_id,
            archive_sha256=archive_sha,
            archive_name=archive_name,
            size_bytes=size_bytes,
            manifest_json=manifest,
        )

        if local:
            ok, msg = verify_local_file(Path(remote_archive), archive_sha)
        else:
            assert ip is not None
            remote_digest = await sshutil.remote_sha256(
                settings, ip, remote_archive, timeout=min(600.0, archive_timeout)
            )
            ok = remote_digest == archive_sha.lower()
            msg = "Checksum OK" if ok else f"Mismatch {remote_digest[:12]}…"
        await log(f"Verify Host-Staging: {msg}")
        if not ok:
            hop = _hop_entry(
                host_dest or {"kind": KIND_HOST, "label": "Host-Staging"},
                status="failed",
                verify="failed",
                path=remote_archive,
            )
            hop_results.append(hop)
            await _apply_legacy_fields(store, run_id, hop)
            raise BackupError(f"Host-Staging-Verify fehlgeschlagen: {msg}")

        hop = _hop_entry(
            host_dest or {"kind": KIND_HOST, "label": "Host-Staging (ephemer)"},
            status="ok",
            verify="ok",
            path=remote_archive,
        )
        hop_results.append(hop)
        await _apply_legacy_fields(store, run_id, hop)
        await store.update_run(run_id, destinations_json=hop_results)

        # --- Restart stack before long copies if we stopped ---
        if stopped:
            await log("Stack wieder starten …")
            await _compose_up(settings, ip, project, inventory, local=local, timeout=timeout)
            started = True
            stopped = False

        # --- Durable hops in order ---
        n_dur = len(durable)
        for idx, dest in enumerate(durable):
            kind = dest.get("kind")
            label = dest.get("label") or kind
            pct = 65 + int(25 * (idx / max(n_dur, 1)))
            await _emit_progress(
                on_progress,
                phase=f"→ {label}",
                percent=pct,
                message=f"Kopiere nach {label}…",
                run_id=run_id,
            )

            try:
                if kind == KIND_COPILOT:
                    base_path = Path(dest.get("remote_path") or str(bsettings.copilot_dir))
                    base_path.mkdir(parents=True, exist_ok=True)
                    dest_dir = base_path / project
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    copilot_path = dest_dir / archive_name
                    await log(f"Kopiere nach Copilot: {copilot_path}")
                    if local_source and local_source.is_file():
                        await sshutil.local_run_ok(
                            f"cp -f -- {shlex.quote(str(local_source))} "
                            f"{shlex.quote(str(copilot_path))}",
                            timeout=transfer_timeout,
                        )
                    elif local:
                        await sshutil.local_run_ok(
                            f"cp -f -- {shlex.quote(remote_archive)} "
                            f"{shlex.quote(str(copilot_path))}",
                            timeout=transfer_timeout,
                        )
                    else:
                        assert ip is not None
                        await sshutil.scp_get(
                            settings,
                            ip,
                            remote_archive,
                            copilot_path,
                            timeout=transfer_timeout,
                        )
                    ok, msg = verify_local_file(copilot_path, archive_sha)
                    await log(f"Verify {label}: {msg}")
                    hop = _hop_entry(
                        dest,
                        status="ok" if ok else "failed",
                        verify="ok" if ok else "failed",
                        path=str(copilot_path),
                    )
                    hop_results.append(hop)
                    await _apply_legacy_fields(store, run_id, hop)
                    await store.update_run(run_id, destinations_json=hop_results)
                    if not ok:
                        raise BackupError(f"{label}-Verify fehlgeschlagen: {msg}")
                    local_source = copilot_path
                    keep = int(dest.get("keep_count") or 5)
                    if keep > 0:
                        await _retain_local(dest_dir, keep=keep)
                        await log(f"Retention {label}: max {keep}")

                    if not host_purged:
                        await _delete_paths(
                            settings,
                            ip=ip,
                            local=local,
                            paths=[remote_archive, remote_work],
                            timeout=timeout,
                        )
                        host_purged = True
                        await log("Host-Staging nach Copilot-Kopie gelöscht")
                        # Mark host hop as ephemeral cleaned
                        if hop_results and hop_results[0].get("kind") == KIND_HOST:
                            hop_results[0]["status"] = "cleared"
                            await _apply_legacy_fields(store, run_id, hop_results[0])
                            await store.update_run(run_id, destinations_json=hop_results)

                elif kind == KIND_SFTP:
                    if local_source is None or not local_source.is_file():
                        raise BackupError(
                            f"{label}: keine lokale Copilot-Kopie als Quelle — "
                            "Copilot-Ziel muss vor SFTP in der Pipeline stehen."
                        )
                    host = (dest.get("host") or "").strip()
                    remote_base = (dest.get("remote_path") or "").rstrip("/")
                    if not host or not remote_base:
                        raise BackupError(f"{label}: Host/Pfad unvollständig.")
                    syn_path = f"{remote_base}/{project}/{archive_name}"
                    auth = resolve_auth(dest, settings)
                    await log(f"Kopiere nach {label}: {syn_path}")
                    await sshutil.scp_put(
                        settings,
                        host,
                        local_source,
                        syn_path,
                        timeout=transfer_timeout,
                        username=auth["username"],
                        key=auth.get("key"),
                        key_pem=auth.get("key_pem"),
                        password=auth.get("password"),
                        port=auth["port"],
                    )
                    remote_digest = await sshutil.remote_sha256(
                        settings,
                        host,
                        syn_path,
                        timeout=min(600.0, transfer_timeout),
                        username=auth["username"],
                        key=auth.get("key"),
                        key_pem=auth.get("key_pem"),
                        password=auth.get("password"),
                        port=auth["port"],
                    )
                    ok = remote_digest == archive_sha.lower()
                    await log(
                        f"Verify {label}: {'OK' if ok else 'Checksum-Mismatch'}"
                    )
                    hop = _hop_entry(
                        dest,
                        status="ok" if ok else "failed",
                        verify="ok" if ok else "failed",
                        path=syn_path,
                    )
                    hop_results.append(hop)
                    await _apply_legacy_fields(store, run_id, hop)
                    await store.update_run(run_id, destinations_json=hop_results)
                    if ok:
                        keep = int(dest.get("keep_count") or 10)
                        if keep > 0:
                            await _retain_remote(
                                settings,
                                ip=host,
                                local=False,
                                directory=f"{remote_base}/{project}",
                                keep=keep,
                                timeout=timeout,
                                username=auth["username"],
                                key=auth.get("key"),
                                key_pem=auth.get("key_pem"),
                                password=auth.get("password"),
                                port=auth["port"],
                            )
                            await log(f"Retention {label}: max {keep}")
                    else:
                        await log(f"{label}-Verify fehlgeschlagen — Run wird partial")
                else:
                    await log(f"Unbekannter Zieltyp {kind} — übersprungen")
                    hop = _hop_entry(dest, status="skipped", verify="skipped")
                    hop_results.append(hop)
                    await store.update_run(run_id, destinations_json=hop_results)
            except BackupError:
                raise
            except Exception as exc:
                # Durable remote failures after first durable OK → partial
                if kind == KIND_COPILOT:
                    hop = _hop_entry(
                        dest, status="failed", verify="failed", path=""
                    )
                    hop_results.append(hop)
                    await _apply_legacy_fields(store, run_id, hop)
                    await store.update_run(run_id, destinations_json=hop_results)
                    raise BackupError(f"{label} fehlgeschlagen: {exc}") from exc
                await log(f"{label} fehlgeschlagen: {exc}")
                hop = _hop_entry(dest, status="failed", verify="failed")
                hop_results.append(hop)
                await _apply_legacy_fields(store, run_id, hop)
                await store.update_run(run_id, destinations_json=hop_results)

        await _emit_progress(
            on_progress,
            phase="Verify",
            percent=95,
            message="Gesamt-Verify…",
            run_id=run_id,
        )
        verify_status, verify_detail = summarize_hop_verifies(hop_results)
        durable_statuses = [
            h.get("status") for h in hop_results if h.get("kind") != KIND_HOST
        ]
        host_ok = any(
            h.get("kind") == KIND_HOST and h.get("verify") == "ok"
            for h in hop_results
        )
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
        elif host_ok and copilot_ok:
            final_status = "success"
        else:
            final_status = "failed"

        now = now_berlin()
        await store.update_run(
            run_id,
            status=final_status,
            finished_at=format_de(now),
            finished_at_iso=iso_utc(now),
            verify_status=verify_status,
            verify_detail=verify_detail,
            destinations_json=hop_results,
        )
        await log(f"Fertig — Status: {final_status}")
        await _emit_progress(
            on_progress,
            phase="Fertig",
            percent=100,
            message=f"Fertig — Status: {final_status}",
            run_id=run_id,
        )
        return await store.get_run(run_id) or {"id": run_id, "status": final_status}

    except Exception as exc:
        msg = getattr(exc, "message", None) or str(exc)
        await log(f"Fehler: {msg}")
        now = now_berlin()
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
        )
        # Ensure legacy lxc_status reflects failure if still pending
        if not any(h.get("kind") == KIND_HOST for h in hop_results):
            await store.update_run(run_id, lxc_status="failed")
        await _emit_progress(
            on_progress,
            phase="Fehler",
            percent=100,
            message=msg,
            run_id=run_id,
        )
        raise
    finally:
        if stopped and not started:
            try:
                await log("Notfall: Stack nach Fehler wieder starten …")
                await _compose_up(
                    settings, ip, project, inventory, local=local, timeout=timeout
                )
            except Exception:
                logger.exception("Failed to restart stack after backup error")


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


async def _create_archive(
    settings: Settings,
    *,
    inventory: dict[str, Any],
    remote_work: str,
    remote_archive: str,
    local: bool,
    ip: str | None,
    quiesced: bool,
    timeout: float,
    archive_timeout: float,
    log: LogFn,
    on_progress: ProgressFn | None = None,
    run_id: int | None = None,
) -> tuple[dict[str, Any], str, int]:
    """Build work dir + tar.gz on target host. Returns manifest, sha256, size.

    Long tar work runs detached on the host (nohup) and is polled with short
    SSH calls so a 120s SSH command timeout cannot abort large Paperless binds.
    """
    progress_path = f"{remote_work.rstrip('/')}/.hc_job_archive.progress"
    script = _build_remote_script(
        inventory=inventory,
        remote_work=remote_work,
        remote_archive=remote_archive,
        quiesced=quiesced,
        progress_path=progress_path,
    )
    await log(
        f"Archiv-Job gestartet (Hintergrund, Timeout {int(archive_timeout)}s)…"
    )
    await sshutil.run_detached_and_poll(
        settings,
        ip,
        script,
        work_dir=remote_work,
        job_name="archive",
        local=local,
        overall_timeout=archive_timeout,
        poll_interval=5.0,
        short_timeout=min(60.0, timeout),
        log=log,
    )
    await _emit_progress(
        on_progress,
        phase="Archive",
        percent=48,
        message="Archiv fertig — Manifest lesen…",
        run_id=run_id,
    )

    result_file = f"{remote_work.rstrip('/')}/.archive_result"
    read_meta = f"cat {shlex.quote(remote_work + '/manifest.json')}"
    read_result = f"cat {shlex.quote(result_file)}"
    if local:
        meta_out = await sshutil.local_run_ok(read_meta, timeout=30)
        result_out = await sshutil.local_run_ok(read_result, timeout=30)
    else:
        assert ip is not None
        meta_out = await sshutil.ssh_run_ok(settings, ip, read_meta, timeout=30)
        result_out = await sshutil.ssh_run_ok(settings, ip, read_result, timeout=30)

    manifest = json.loads(meta_out)
    parts = result_out.strip().split()
    if len(parts) < 2:
        raise BackupError(f"Archiv-Ergebnis ungültig: {result_out!r}")
    archive_sha = parts[0].lower()
    size_bytes = int(parts[1])
    manifest["archive_sha256"] = archive_sha
    ok, msg = validate_manifest(manifest)
    if not ok:
        await log(f"Manifest-Warnung: {msg}")
    await log(
        f"Archiv {Path(remote_archive).name} · {size_bytes} Bytes · "
        f"SHA256 {archive_sha[:16]}…"
    )
    return manifest, archive_sha, size_bytes


def _build_remote_script(
    *,
    inventory: dict[str, Any],
    remote_work: str,
    remote_archive: str,
    quiesced: bool,
    progress_path: str = "",
) -> str:
    """Bash script executed on LXC (or local) to assemble backup contents."""
    project = inventory["stack"]
    wd = inventory.get("working_dir") or ""
    prog = progress_path or f"{remote_work.rstrip('/')}/.hc_job_archive.progress"

    def _prog(msg: str) -> str:
        return f'printf "%s\\n" {shlex.quote(msg)} > {shlex.quote(prog)}'

    lines = [
        "set -euo pipefail",
        f"WORK={shlex.quote(remote_work)}",
        f"ARCH={shlex.quote(remote_archive)}",
        _prog("prepare"),
        'mkdir -p "$WORK/compose" "$WORK/volumes" "$WORK/binds"',
        f'mkdir -p "$(dirname "$ARCH")"',
    ]

    for cf in inventory.get("compose_files") or []:
        lines.append(
            f'cp -a -- {shlex.quote(cf)} "$WORK/compose/" 2>/dev/null || true'
        )
    if inventory.get("env_file"):
        lines.append(
            f'cp -a -- {shlex.quote(inventory["env_file"])} "$WORK/compose/.env" 2>/dev/null || true'
        )

    if wd:
        lines.append(
            f"(cd {shlex.quote(wd)} && docker compose config) "
            f'> "$WORK/compose/compose-config.yml" 2>/dev/null || '
            f"docker compose -p {shlex.quote(project)} config "
            f'> "$WORK/compose/compose-config.yml" 2>/dev/null || true'
        )
    else:
        lines.append(
            f"docker compose -p {shlex.quote(project)} config "
            f'> "$WORK/compose/compose-config.yml" 2>/dev/null || true'
        )

    for vol in inventory.get("named_volumes") or []:
        name = vol["name"]
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name)
        lines.append(_prog(f"volume:{safe}"))
        # Prefer host mountpoint (no pull); fall back to alpine/busybox helper
        lines.append(
            f'MP=$(docker volume inspect -f "{{{{.Mountpoint}}}}" {shlex.quote(name)} 2>/dev/null || true)\n'
            f'if [ -n "$MP" ] && [ -d "$MP" ]; then\n'
            f'  tar czf "$WORK/volumes/{safe}.tar.gz" -C "$MP" .\n'
            f'else\n'
            f'  docker run --rm -v {shlex.quote(name)}:/v:ro -v "$WORK/volumes:/out" '
            f'alpine:3.20 tar czf /out/{safe}.tar.gz -C /v . '
            f'2>/dev/null || '
            f'  docker run --rm -v {shlex.quote(name)}:/v:ro -v "$WORK/volumes:/out" '
            f'busybox:1.36 tar czf /out/{safe}.tar.gz -C /v .\n'
            f'fi'
        )

    for bind in inventory.get("bind_mounts") or []:
        if not bind.get("readable"):
            continue
        src = bind["source"]
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", src.strip("/"))[:80] or "bind"
        lines.append(_prog(f"bind:{safe}"))
        lines.append(
            f'if [ -d {shlex.quote(src)} ]; then '
            f'tar czf "$WORK/binds/{safe}.tar.gz" -C {shlex.quote(src)} . ; '
            f'elif [ -f {shlex.quote(src)} ]; then '
            f'tar czf "$WORK/binds/{safe}.tar.gz" -C "$(dirname {shlex.quote(src)})" '
            f'"$(basename {shlex.quote(src)})" ; '
            f'fi'
        )

    now = now_berlin()
    mounts = []
    for vol in inventory.get("named_volumes") or []:
        mounts.append({"type": "volume", **vol})
    for bind in inventory.get("bind_mounts") or []:
        mounts.append({"type": "bind", **bind})

    # Placeholder sha — rewritten after first pack (same as before)
    manifest = {
        "stack": project,
        "parent_id": inventory["parent_id"],
        "guest_name": inventory["guest_name"],
        "created_at": format_de(now),
        "created_at_iso": iso_utc(now),
        "containers": inventory.get("containers") or [],
        "working_dir": wd or None,
        "compose_files": inventory.get("compose_files") or [],
        "env_file": inventory.get("env_file"),
        "mounts": mounts,
        "quiesced": quiesced,
        "warnings": inventory.get("warnings") or [],
        "gaps": inventory.get("gaps") or [],
        "archive_sha256": "0" * 64,
        "checksums": {},
    }
    manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)
    lines.append(_prog("checksums"))
    lines.append(
        f"cat > \"$WORK/manifest.json\" << 'EOFMANIFEST'\n{manifest_json}\nEOFMANIFEST"
    )
    lines.append(
        'find "$WORK" -type f ! -name manifest.json -print0 | while IFS= read -r -d "" f; do '
        'rel="${f#"$WORK"/}"; '
        'sum=$(sha256sum -- "$f" | awk \'{print $1}\'); '
        'echo "$rel $sum"; '
        "done > \"$WORK/checksums.txt\" || true"
    )
    lines.append(_prog("pack"))
    lines.append('tar -czf "$ARCH" -C "$WORK" .')
    # Update manifest with first-pack sha, re-pack (matches previous Python two-step)
    lines.append(
        'SHA1=$(sha256sum -- "$ARCH" | awk \'{print $1}\')\n'
        'sed -i "s/\\"archive_sha256\\": \\"[a-fA-F0-9]*\\"/\\"archive_sha256\\": \\"$SHA1\\"/" '
        '"$WORK/manifest.json"'
    )
    lines.append(_prog("repack"))
    lines.append('tar -czf "$ARCH" -C "$WORK" .')
    lines.append(
        'SHA2=$(sha256sum -- "$ARCH" | awk \'{print $1}\')\n'
        'SIZE=$(stat -c %s -- "$ARCH" 2>/dev/null || stat -f %z -- "$ARCH")\n'
        'printf "%s %s\\n" "$SHA2" "$SIZE" > "$WORK/.archive_result"'
    )
    lines.append(_prog("done"))
    return "\n".join(lines)


async def _retain_local(directory: Path, *, keep: int) -> None:
    if not directory.is_dir():
        return
    files = sorted(
        [p for p in directory.glob("*.tar.gz") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            logger.warning("Konnte altes Backup nicht löschen: %s", old)


async def _retain_remote(
    settings: Settings,
    *,
    ip: str | None,
    local: bool,
    directory: str,
    keep: int,
    timeout: float,
    username: str | None = None,
    key: Path | None = None,
    key_pem: str | None = None,
    password: str | None = None,
    port: int | None = None,
) -> None:
    # List tar.gz by mtime, delete oldest beyond keep
    script = f"""
set -e
DIR={shlex.quote(directory)}
KEEP={int(keep)}
[ -d "$DIR" ] || exit 0
mapfile -t FILES < <(ls -1t "$DIR"/*.tar.gz 2>/dev/null || true)
COUNT=${{#FILES[@]}}
if [ "$COUNT" -le "$KEEP" ]; then exit 0; fi
for ((i=KEEP; i<COUNT; i++)); do
  rm -f -- "${{FILES[$i]}}"
done
"""
    if local:
        await sshutil.local_run(script, timeout=timeout)
    else:
        assert ip is not None
        await sshutil.ssh_run(
            settings,
            ip,
            script,
            timeout=timeout,
            username=username,
            key=key,
            key_pem=key_pem,
            password=password,
            port=port,
        )


async def list_backup_stacks(
    snapshot: TopologySnapshot | None,
) -> list[dict[str, Any]]:
    """Compose projects from topology for UI selectors."""
    if snapshot is None:
        return []
    seen: dict[str, dict[str, Any]] = {}
    for c in snapshot.containers:
        labels = c.labels or {}
        project = labels.get("com.docker.compose.project") or (c.meta or {}).get(
            "compose_project"
        )
        if not project or not c.parent_id:
            continue
        key = f"{c.parent_id}::{project}"
        if key not in seen:
            guest = next(
                (g.name for g in snapshot.guests if g.id == c.parent_id),
                c.parent_id,
            )
            seen[key] = {
                "parent_id": c.parent_id,
                "stack": project,
                "guest_name": guest,
                "containers": 0,
            }
        seen[key]["containers"] += 1
    return sorted(seen.values(), key=lambda x: (x["guest_name"], x["stack"]))
