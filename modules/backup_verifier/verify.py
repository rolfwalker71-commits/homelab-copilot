"""Checksum and manifest verification after each backup hop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backup_verifier.sshutil import local_sha256


REQUIRED_MANIFEST_KEYS = (
    "stack",
    "parent_id",
    "created_at",
    "created_at_iso",
    "archive_sha256",
    "mounts",
)


def validate_manifest(manifest: dict[str, Any] | None) -> tuple[bool, str]:
    if not isinstance(manifest, dict):
        return False, "Manifest fehlt oder ist kein Objekt"
    missing = [k for k in REQUIRED_MANIFEST_KEYS if k not in manifest]
    if missing:
        return False, f"Manifest unvollständig — fehlt: {', '.join(missing)}"
    digest = str(manifest.get("archive_sha256") or "")
    if len(digest) != 64:
        return False, "archive_sha256 im Manifest ungültig"
    return True, "Manifest OK"


def verify_local_file(path: Path, expected_sha256: str) -> tuple[bool, str]:
    if not path.is_file():
        return False, f"Datei fehlt: {path}"
    actual = local_sha256(path)
    if actual.lower() != expected_sha256.lower():
        return False, f"Checksum-Mismatch: erwartet {expected_sha256[:12]}…, ist {actual[:12]}…"
    return True, f"Checksum OK ({actual[:12]}…)"


def verify_manifest_file(path: Path) -> tuple[bool, str, dict[str, Any] | None]:
    if not path.is_file():
        return False, f"Manifest-Datei fehlt: {path}", None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"Manifest lesefehler: {exc}", None
    ok, msg = validate_manifest(data)
    return ok, msg, data if ok else data


def summarize_hop_verifies(
    *,
    lxc: str | None,
    copilot: str | None,
    synology: str | None,
) -> tuple[str, str]:
    """Return overall verify_status + detail string."""
    parts = {
        "lxc": lxc or "pending",
        "copilot": copilot or "pending",
        "synology": synology or "pending",
    }
    detail = ", ".join(f"{k}={v}" for k, v in parts.items())
    values = list(parts.values())
    if any(v == "failed" for v in values):
        return "failed", detail
    # skipped synology is OK for partial/success
    critical = [parts["lxc"], parts["copilot"]]
    if all(v == "ok" for v in critical):
        if parts["synology"] in ("ok", "skipped"):
            return "ok", detail
        return "partial", detail
    if any(v == "pending" for v in values):
        return "pending", detail
    return "partial", detail
