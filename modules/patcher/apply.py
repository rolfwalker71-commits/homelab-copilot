"""Apply package updates over SSH (apt / dnf / yum / apk)."""

from __future__ import annotations

import logging
import shlex
from typing import Any

from patcher.config import get_patcher_settings
from patcher.detect import check_reboot_required, detect_host
from patcher.sshutil import ssh_run
from patcher.targets import PatchTarget

logger = logging.getLogger(__name__)


class ApplyError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _safe_pkg_list(names: list[str]) -> list[str]:
    out: list[str] = []
    for n in names:
        n = (n or "").strip()
        if not n:
            continue
        # reject shell metacharacters
        if any(c in n for c in " \t\n;&|`$<>(){}[]\\\"'"):
            raise ApplyError(f"Ungültiger Paketname: {n}")
        out.append(n)
    return out


async def apply_updates(
    target: PatchTarget,
    *,
    package_filter: str = "all",
    packages: list[str] | None = None,
) -> dict[str, Any]:
    """Install updates. package_filter: security | all | selected."""
    ps = get_patcher_settings()
    timeout = ps.patcher_apply_timeout
    connect_timeout = ps.patcher_connect_timeout

    detect = await detect_host(
        target, timeout=min(60.0, timeout), connect_timeout=connect_timeout
    )
    pm = detect.pm
    filt = (package_filter or "all").lower()
    selected = _safe_pkg_list(packages or [])

    if filt == "selected" and not selected:
        raise ApplyError("Keine Pakete für Filter „selected“ angegeben.")

    if pm == "apt":
        cmd = _apt_cmd(filt, selected)
    elif pm in ("dnf", "yum"):
        cmd = _dnf_cmd(pm, filt, selected)
    elif pm == "apk":
        cmd = _apk_cmd(filt, selected)
    else:
        raise ApplyError(f"Nicht unterstützter Paketmanager: {pm}")

    stdout, stderr, code = await ssh_run(
        target.ip,
        cmd,
        timeout=timeout,
        username=target.ssh_user,
        port=target.port,
        connect_timeout=connect_timeout,
    )
    log = ((stdout or "") + ("\n" + stderr if stderr else "")).strip()
    if code != 0:
        raise ApplyError(
            f"Update fehlgeschlagen (exit {code}): "
            + (stderr or stdout or "")[:800]
        )

    reboot = await check_reboot_required(
        target, timeout=30.0, connect_timeout=connect_timeout
    )
    return {
        "pm": pm,
        "distro": detect.pretty_name,
        "log": log[-8000:],
        "reboot_required": reboot,
        "exit_code": code,
    }


def _apt_cmd(filt: str, selected: list[str]) -> str:
    base = "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; "
    if filt == "selected":
        pkgs = " ".join(shlex.quote(p) for p in selected)
        return base + f"apt-get install -y --only-upgrade {pkgs}"
    if filt == "security":
        # Prefer unattended-upgrade security pocket when available; else full upgrade
        return (
            base
            + "if command -v unattended-upgrade >/dev/null 2>&1; then "
            "unattended-upgrade -v; "
            "else apt-get -y upgrade; fi"
        )
    return base + "apt-get -y upgrade"


def _dnf_cmd(pm: str, filt: str, selected: list[str]) -> str:
    bin_name = "dnf" if pm == "dnf" else "yum"
    if filt == "selected":
        pkgs = " ".join(shlex.quote(p) for p in selected)
        return f"{bin_name} -y upgrade {pkgs}"
    if filt == "security":
        return (
            f"{bin_name} -y update --security 2>/dev/null || "
            f"{bin_name} -y upgrade --security 2>/dev/null || "
            f"{bin_name} -y upgrade"
        )
    return f"{bin_name} -y upgrade"


def _apk_cmd(filt: str, selected: list[str]) -> str:
    if filt == "selected":
        pkgs = " ".join(shlex.quote(p) for p in selected)
        return f"apk update && apk add -u {pkgs}"
    # apk has no native security-only; selected/all → full upgrade
    return "apk update && apk upgrade"


async def reboot_host(target: PatchTarget, *, confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise ApplyError("Reboot erfordert confirm=true.")
    ps = get_patcher_settings()
    # Fire-and-forget style: schedule reboot shortly so SSH can return
    stdout, stderr, code = await ssh_run(
        target.ip,
        "nohup sh -c 'sleep 2; reboot' >/dev/null 2>&1 & echo REBOOT_SCHEDULED",
        timeout=30.0,
        username=target.ssh_user,
        port=target.port,
        connect_timeout=ps.patcher_connect_timeout,
    )
    if "REBOOT_SCHEDULED" not in (stdout or "") and code != 0:
        raise ApplyError(
            f"Reboot konnte nicht geplant werden: {(stderr or stdout or '')[:400]}"
        )
    return {"ok": True, "message": "Reboot in wenigen Sekunden…"}
