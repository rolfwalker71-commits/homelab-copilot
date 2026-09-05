"""Nightly restore-drill runner: restic check + cheap local tar list."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from app.core.locale import now_berlin

from backup_verifier.browser import ARCHIVE_SUFFIXES, dest_root, join_under_root
from backup_verifier.config import BackupSettings, get_backup_settings
from backup_verifier.destinations import KIND_SFTP, ensure_seeded
from backup_verifier.drill import (
    ENGINE_RESTIC,
    ENGINE_TAR,
    STATUS_FAILED,
    evaluate_tar_list,
    should_push_drill,
    summarize_drill_batch,
)
from backup_verifier.restic import copilot_repo_path, restic_check_local
from backup_verifier.restore import list_tar_members, RestoreError
from backup_verifier.store import BackupStore

logger = logging.getLogger(__name__)


async def run_nightly_drill(
    store: BackupStore,
    *,
    bsettings: BackupSettings | None = None,
    include_dest: bool | None = None,
) -> dict[str, Any]:
    """Inspect local restic repos + cheap local tar archives. Persist results."""
    bsettings = bsettings or get_backup_settings()
    include_dest = (
        bsettings.backup_drill_dest if include_dest is None else include_dest
    )
    prev = await store.latest_drill_summary()
    prev_status = (prev or {}).get("last_status") or None
    results: list[dict[str, Any]] = []

    secrets = await _list_restic_secrets(store)
    for meta in secrets:
        parent_id = str(meta.get("parent_id") or "")
        project = str(meta.get("project") or "")
        password = await store.get_restic_password(parent_id, project)
        repo = copilot_repo_path(bsettings.copilot_dir, parent_id, project)
        key = f"restic:{parent_id}/{project}"
        label = f"restic {project} @ Copilot"
        rid = await store.create_drill_run(
            engine=ENGINE_RESTIC, target_key=key, dest_label=label
        )
        t0 = time.time()
        if not password:
            out = {
                "ok": False,
                "status": STATUS_FAILED,
                "detail": "Kein restic-Passwort gespeichert.",
            }
        else:
            out = await restic_check_local(
                repo, password, timeout=bsettings.backup_drill_timeout
            )
        await store.finish_drill_run(
            rid,
            status=out["status"],
            detail=out.get("detail") or "",
            duration_s=time.time() - t0,
        )
        results.append({**out, "target_key": key, "dest_label": label, "engine": ENGINE_RESTIC})

    if include_dest:
        await ensure_seeded(store)
        for dest in await store.list_destinations():
            if dest.get("kind") != KIND_SFTP or not dest.get("enabled"):
                continue
            # Cheap: dest restic/config exists? Do not download packs or archives.
            for meta in secrets:
                parent_id = str(meta.get("parent_id") or "")
                project = str(meta.get("project") or "")
                key = f"restic-dest:{dest.get('id')}:{parent_id}/{project}"
                label = f"restic {project} @ {dest.get('label') or dest.get('kind')}"
                rid = await store.create_drill_run(
                    engine=ENGINE_RESTIC, target_key=key, dest_label=label
                )
                t0 = time.time()
                try:
                    from backup_verifier import sshutil
                    from backup_verifier.destinations import resolve_auth
                    from backup_verifier.restic import sftp_repo_rel
                    from app.config import get_settings

                    settings = get_settings()
                    root = dest_root(dest, bsettings)
                    remote = join_under_root(root, sftp_repo_rel(parent_id, project) + "/config")
                    auth = resolve_auth(dest, settings)
                    await sshutil.sftp_stat_file(
                        settings,
                        dest["host"],
                        remote,
                        username=auth["username"],
                        key=auth.get("key"),
                        key_pem=auth.get("key_pem"),
                        password=auth.get("password"),
                        port=auth["port"],
                    )
                    out = {
                        "ok": True,
                        "status": "success",
                        "detail": "Dest-Repo config erreichbar (kein Pack-Download).",
                    }
                except Exception as exc:
                    msg = getattr(exc, "message", None) or str(exc)
                    out = {
                        "ok": False,
                        "status": STATUS_FAILED,
                        "detail": f"Dest-Repo nicht prüfbar: {msg}"[:240],
                    }
                await store.finish_drill_run(
                    rid,
                    status=out["status"],
                    detail=out.get("detail") or "",
                    duration_s=time.time() - t0,
                )
                results.append(
                    {**out, "target_key": key, "dest_label": label, "engine": ENGINE_RESTIC}
                )

    # Cheap local tar: newest archive per stack dir under Copilot.
    root = Path(bsettings.copilot_dir)
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name in {"restic", "_restore"}:
                continue
            newest = _newest_local_archive(child)
            if newest is None:
                continue
            key = f"tar:{child.name}/{newest.name}"
            label = f"tar {child.name} @ Copilot"
            rid = await store.create_drill_run(
                engine=ENGINE_TAR, target_key=key, dest_label=label
            )
            t0 = time.time()
            try:
                members = list_tar_members(newest, limit=80)
                out = evaluate_tar_list(
                    readable=True, member_count=len(members), downloaded=False
                )
            except RestoreError as exc:
                out = evaluate_tar_list(readable=False, error=exc.message)
            except OSError as exc:
                out = evaluate_tar_list(readable=False, error=str(exc))
            await store.finish_drill_run(
                rid,
                status=out["status"],
                detail=out.get("detail") or "",
                duration_s=time.time() - t0,
            )
            results.append({**out, "target_key": key, "dest_label": label, "engine": ENGINE_TAR})

    summary = summarize_drill_batch(results)
    push_kind = should_push_drill(prev_status, summary["status"]) or ""
    today = now_berlin().strftime("%Y-%m-%d")
    await store.set_drill_state(
        fired_date=today,
        status=summary["status"],
        summary={**summary, "results": results[:40]},
        push_kind=push_kind,
    )
    return {
        "ok": summary["status"] != STATUS_FAILED,
        **summary,
        "push_kind": push_kind,
        "results": results,
        "time": now_berlin().isoformat(),
    }


async def _list_restic_secrets(store: BackupStore) -> list[dict[str, Any]]:
    db = store._require()
    async with db.execute(
        "SELECT parent_id, project FROM restic_secrets ORDER BY project"
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


def _newest_local_archive(folder: Path) -> Path | None:
    files = [
        p
        for p in folder.iterdir()
        if p.is_file() and p.name.lower().endswith(ARCHIVE_SUFFIXES)
    ]
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0]


async def notify_drill_finished(result: dict[str, Any]) -> None:
    kind = result.get("push_kind")
    if not kind:
        return
    try:
        from app.core.push import push_allowed, send_push_to_all
        from app.main import app as fastapi_app

        store = getattr(fastapi_app.state, "app_store", None)
        if store is None:
            return
        if not await push_allowed(store, "backup_failure"):
            return
        fail_n = int(result.get("fail_count") or 0)
        if kind == "fail":
            title = "HomelabOps — Backup-Drill fehlgeschlagen"
            body = f"{fail_n} Prüfung(en) fehlgeschlagen. Backup-Seite öffnen."
        else:
            title = "HomelabOps — Backup-Drill wieder OK"
            body = "Restore-Drill nach Fehler wieder erfolgreich."
        await send_push_to_all(
            store,
            title=title,
            body=body,
            url="/modules/backup_verifier",
            tag="backup-drill",
        )
    except Exception:
        logger.exception("Push (Restore-Drill) fehlgeschlagen")
