"""Heuristic priority for pending packages (security vs normal)."""

from __future__ import annotations

import re
from typing import Any


_SECURITY_NAME = re.compile(
    r"(security|sec-|ubuntu-security|debian-security|updates/security)",
    re.I,
)
_SECURITY_HINTS = re.compile(
    r"(linux-image|linux-headers|openssl|openssh|sudo|systemd|glibc|libc6|"
    r"kernel|firefox|chromium|curl|wget|gnutls|nss)",
    re.I,
)


def classify_package(
    name: str,
    *,
    archive: str | None = None,
    repo: str | None = None,
    severity: str | None = None,
    meta: dict[str, Any] | None = None,
) -> str:
    """Return ``security`` or ``normal``."""
    meta = meta or {}
    blob = " ".join(
        str(x)
        for x in (
            name,
            archive,
            repo,
            severity,
            meta.get("archive"),
            meta.get("repo"),
            meta.get("origin"),
            meta.get("section"),
        )
        if x
    )
    if severity and severity.lower() in ("critical", "important", "high", "security"):
        return "security"
    if _SECURITY_NAME.search(blob):
        return "security"
    if "security" in blob.lower():
        return "security"
    # Soft hint for well-known security-sensitive packages when only name known
    if archive is None and repo is None and _SECURITY_HINTS.search(name):
        return "normal"  # don't over-flag without repo evidence
    return "normal"


def summarize_packages(packages: list[dict[str, Any]]) -> dict[str, Any]:
    security = sum(1 for p in packages if p.get("priority") == "security")
    normal = len(packages) - security
    return {
        "total": len(packages),
        "security": security,
        "normal": normal,
        "top": [
            {
                "name": p.get("name"),
                "current": p.get("current"),
                "candidate": p.get("candidate"),
                "priority": p.get("priority"),
            }
            for p in packages[:15]
        ],
    }
