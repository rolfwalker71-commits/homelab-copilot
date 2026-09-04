"""Backup destination pipeline: CRUD helpers, seed, auth, connection check."""

from __future__ import annotations

import logging
import shlex
import tempfile
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.core.docker_control import DockerControlError, ssh_key_path

from backup_verifier.config import BackupSettings, get_backup_settings
from backup_verifier import sshutil
from backup_verifier.store import BackupStore

logger = logging.getLogger(__name__)

KIND_HOST = "host_staging"
KIND_COPILOT = "copilot"
KIND_SFTP = "sftp"

AUTH_PASSWORD = "password"
AUTH_KEY_DOCKER = "key_docker"
AUTH_KEY_PATH = "key_path"
AUTH_KEY_PEM = "key_pem"

PRESETS = ("synology", "storage_box", "custom")


def public_destination(row: dict[str, Any]) -> dict[str, Any]:
    """API-safe view (no raw secrets)."""
    secret = row.get("secret_ref") or ""
    return {
        "id": row.get("id"),
        "sort_order": int(row.get("sort_order") or 0),
        "enabled": bool(row.get("enabled")),
        "kind": row.get("kind"),
        "label": row.get("label") or "",
        "preset": row.get("preset") or "custom",
        "host": row.get("host") or "",
        "port": int(row.get("port") or 22),
        "username": row.get("username") or "",
        "remote_path": row.get("remote_path") or "",
        "auth_mode": row.get("auth_mode") or AUTH_KEY_DOCKER,
        "has_secret": bool(str(secret).strip()),
        "keep_count": int(row.get("keep_count") or 5),
        "ephemeral": row.get("kind") == KIND_HOST,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def default_seed_rows(bs: BackupSettings | None = None) -> list[dict[str, Any]]:
    bs = bs or get_backup_settings()
    rows: list[dict[str, Any]] = [
        {
            "sort_order": 0,
            "enabled": True,
            "kind": KIND_HOST,
            "label": "Host-Staging (ephemer)",
            "preset": "custom",
            "host": "",
            "port": 22,
            "username": "",
            "remote_path": bs.backup_lxc_dir,
            "auth_mode": AUTH_KEY_DOCKER,
            "secret_ref": "",
            "keep_count": 0,
        },
        {
            "sort_order": 1,
            "enabled": True,
            "kind": KIND_COPILOT,
            "label": "Copilot",
            "preset": "custom",
            "host": "",
            "port": 22,
            "username": "",
            "remote_path": str(bs.copilot_dir),
            "auth_mode": AUTH_KEY_DOCKER,
            "secret_ref": "",
            "keep_count": bs.backup_copilot_keep,
        },
    ]
    if bs.synology_configured:
        auth = AUTH_KEY_PATH if bs.backup_synology_key_path else AUTH_KEY_DOCKER
        secret = bs.backup_synology_key_path if auth == AUTH_KEY_PATH else ""
        rows.append(
            {
                "sort_order": 2,
                "enabled": True,
                "kind": KIND_SFTP,
                "label": "Synology",
                "preset": "synology",
                "host": bs.backup_synology_host,
                "port": bs.backup_synology_port,
                "username": bs.backup_synology_user,
                "remote_path": bs.backup_synology_path,
                "auth_mode": auth,
                "secret_ref": secret,
                "keep_count": bs.backup_synology_keep,
            }
        )
    return rows


async def ensure_seeded(store: BackupStore, bs: BackupSettings | None = None) -> None:
    existing = await store.list_destinations()
    if existing:
        return
    bs = bs or get_backup_settings()
    await store.replace_destinations(default_seed_rows(bs))
    logger.info("backup_destinations seeded from defaults/env")


async def get_pipeline(store: BackupStore) -> list[dict[str, Any]]:
    await ensure_seeded(store)
    rows = await store.list_destinations()
    return [r for r in rows if r.get("enabled")]


def resolve_auth(
    dest: dict[str, Any],
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Return kwargs for sshutil: username, port, password?, key?, key_pem?"""
    settings = settings or get_settings()
    mode = dest.get("auth_mode") or AUTH_KEY_DOCKER
    secret = (dest.get("secret_ref") or "").strip()
    out: dict[str, Any] = {
        "username": (dest.get("username") or settings.docker_ssh_user or "root"),
        "port": int(dest.get("port") or 22),
        "password": None,
        "key": None,
        "key_pem": None,
    }
    if mode == AUTH_PASSWORD:
        if not secret:
            raise DockerControlError(
                "Passwort-Auth gewählt, aber kein Passwort hinterlegt. "
                "Passwort eintragen, „In Liste übernehmen“ und Speichern — "
                "„Leer lassen“ behält nur ein bereits gespeichertes Secret.",
                status_code=400,
            )
        out["password"] = secret
    elif mode == AUTH_KEY_PATH:
        p = Path(secret) if secret else ssh_key_path(settings)
        out["key"] = p
    elif mode == AUTH_KEY_PEM:
        if not secret:
            raise DockerControlError(
                "PEM-Auth gewählt, aber kein Private Key hinterlegt.",
                status_code=400,
            )
        out["key_pem"] = secret
    else:
        out["key"] = ssh_key_path(settings)
    return out


def _restricted_ssh_shell(dest: dict[str, Any], port: int) -> bool:
    """Hetzner Storage Box port 23: limited shell (no pipes/&&, no ``test``)."""
    preset = (dest.get("preset") or "").strip()
    return preset == "storage_box" or port == 23


async def check_destination(
    dest: dict[str, Any],
    *,
    settings: Settings | None = None,
    bsettings: BackupSettings | None = None,
) -> dict[str, Any]:
    """Probe connectivity for a destination (may be unsaved)."""
    settings = settings or get_settings()
    bsettings = bsettings or get_backup_settings()
    kind = dest.get("kind")
    label = dest.get("label") or kind

    if kind == KIND_HOST:
        path = (dest.get("remote_path") or bsettings.backup_lxc_dir).rstrip("/")
        return {
            "ok": True,
            "kind": kind,
            "message": (
                f"Host-Staging ist ephemer — Pfad auf dem Guest: {path}. "
                "Erreichbarkeit wird beim Backup per Guest-SSH geprüft."
            ),
        }

    if kind == KIND_COPILOT:
        path = Path(dest.get("remote_path") or str(bsettings.copilot_dir))
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".hc_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return {
                "ok": True,
                "kind": kind,
                "message": f"Copilot-Pfad schreibbar: {path}",
            }
        except OSError as exc:
            return {
                "ok": False,
                "kind": kind,
                "message": f"Copilot-Pfad nicht schreibbar ({path}): {exc}",
            }

    if kind != KIND_SFTP:
        return {"ok": False, "kind": kind, "message": f"Unbekannter Typ: {kind}"}

    host = (dest.get("host") or "").strip()
    remote_path = (dest.get("remote_path") or "").strip()
    if not host or not remote_path:
        return {
            "ok": False,
            "kind": kind,
            "message": "Host und Remote-Pfad sind für SFTP erforderlich.",
        }

    try:
        auth = resolve_auth(dest, settings)
    except DockerControlError as exc:
        return {"ok": False, "kind": kind, "message": exc.message}

    timeout = min(30.0, bsettings.backup_ssh_timeout)
    probe = remote_path.rstrip("/") + "/.hc_probe"
    run_kw = dict(
        username=auth["username"],
        key=auth.get("key"),
        key_pem=auth.get("key_pem"),
        password=auth.get("password"),
        port=auth["port"],
        timeout=timeout,
    )
    try:
        # Full shell (Synology): one compound command is fine.
        # Storage Box port 23: no && / test — run simple whitelisted cmds only.
        if _restricted_ssh_shell(dest, auth["port"]):
            base = remote_path.rstrip("/")
            if base not in ("/home", "home", ".", ""):
                # mkdir may fail if dir exists; ignore — touch proves write access
                try:
                    await sshutil.ssh_run_ok(
                        settings,
                        host,
                        f"mkdir {shlex.quote(base)}",
                        **run_kw,
                    )
                except DockerControlError:
                    pass
            await sshutil.ssh_run_ok(
                settings, host, f"touch {shlex.quote(probe)}", **run_kw
            )
            await sshutil.ssh_run_ok(
                settings, host, f"rm {shlex.quote(probe)}", **run_kw
            )
        else:
            await sshutil.ssh_run_ok(
                settings,
                host,
                f"mkdir -p -- {shlex.quote(remote_path)} && "
                f"test -w {shlex.quote(remote_path)} && "
                f"touch {shlex.quote(probe)} && "
                f"rm -f -- {shlex.quote(probe)}",
                **run_kw,
            )
        return {
            "ok": True,
            "kind": kind,
            "message": f"SFTP OK — {label} ({host}:{auth['port']} → {remote_path})",
        }
    except DockerControlError as exc:
        msg = exc.message
        low = msg.lower()
        # Avoid duplicating shell/port-23 hints already in format_ssh_failure.
        if (
            _restricted_ssh_shell(dest, auth["port"])
            and "port 23" not in low
            and "ssh-unterstützung" not in low
            and "authentifizierung fehlgeschlagen" not in low
        ):
            msg += (
                " Hinweis: Storage Box braucht Port 23 und „SSH-Unterstützung“ "
                "in der Hetzner Console (Port 22 = nur SFTP ohne Shell)."
            )
        return {"ok": False, "kind": kind, "message": msg}
    except Exception as exc:
        return {"ok": False, "kind": kind, "message": str(exc)}


def normalize_incoming(
    items: list[dict[str, Any]],
    *,
    existing: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Validate + normalize PUT payload; keep old secret if blank."""
    by_id = {int(r["id"]): r for r in (existing or []) if r.get("id") is not None}
    out: list[dict[str, Any]] = []
    seen_host = False
    for i, raw in enumerate(items):
        kind = str(raw.get("kind") or "").strip()
        if kind not in {KIND_HOST, KIND_COPILOT, KIND_SFTP}:
            raise ValueError(f"Ungültiger Zieltyp: {kind}")
        if kind == KIND_HOST:
            if seen_host:
                raise ValueError("Nur ein Host-Staging-Ziel erlaubt.")
            seen_host = True
        auth_mode = str(raw.get("auth_mode") or AUTH_KEY_DOCKER).strip()
        if auth_mode not in {
            AUTH_PASSWORD,
            AUTH_KEY_DOCKER,
            AUTH_KEY_PATH,
            AUTH_KEY_PEM,
        }:
            raise ValueError(f"Ungültiger auth_mode: {auth_mode}")
        preset = str(raw.get("preset") or "custom").strip()
        if preset not in PRESETS:
            preset = "custom"
        secret = raw.get("secret_ref")
        if secret is None or str(secret) == "":
            # Keep previous secret when updating by id
            rid = raw.get("id")
            if rid is not None and int(rid) in by_id:
                secret = by_id[int(rid)].get("secret_ref") or ""
            else:
                secret = ""
        keep = int(raw.get("keep_count") if raw.get("keep_count") is not None else 5)
        if kind == KIND_HOST:
            keep = 0
        keep = max(0, min(keep, 200))
        out.append(
            {
                "id": raw.get("id"),
                "sort_order": int(raw.get("sort_order") if raw.get("sort_order") is not None else i),
                "enabled": bool(raw.get("enabled", True)),
                "kind": kind,
                "label": str(raw.get("label") or kind)[:120],
                "preset": preset,
                "host": str(raw.get("host") or "")[:255],
                "port": int(raw.get("port") or 22),
                "username": str(raw.get("username") or "")[:120],
                "remote_path": str(raw.get("remote_path") or "")[:512],
                "auth_mode": auth_mode,
                "secret_ref": str(secret),
                "keep_count": keep,
            }
        )
    if not any(r["kind"] == KIND_HOST for r in out):
        # Always ensure host staging exists (prepend)
        seed_host = default_seed_rows()[0]
        seed_host["sort_order"] = -1
        out.insert(0, seed_host)
        for i, r in enumerate(out):
            r["sort_order"] = i
    if not any(r["kind"] == KIND_COPILOT and r["enabled"] for r in out):
        raise ValueError("Mindestens ein aktives Copilot-Ziel ist erforderlich.")
    return out


def legacy_role_for(kind: str, preset: str) -> str | None:
    if kind == KIND_HOST:
        return "lxc"
    if kind == KIND_COPILOT:
        return "copilot"
    if kind == KIND_SFTP and preset == "synology":
        return "synology"
    return None
