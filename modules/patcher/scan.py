"""Scan pending package updates over SSH (apt / dnf / yum / apk)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from patcher.config import get_patcher_settings
from patcher.detect import HostDetect, check_reboot_required, detect_host
from patcher.priority import classify_package, summarize_packages
from patcher.sshutil import ssh_run
from patcher.targets import PatchTarget

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, str], Awaitable[None] | None]


class ScanError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


_APT_LINE = re.compile(
    r"^(\S+)/(\S+)\s+(\S+)\s+\S+\s+\[upgradable from:\s*([^\]]+)\]",
    re.I,
)
_DNF_LINE = re.compile(
    r"^(\S+)\s+(\S+)\s+(\S+)\s*$"
)
_APK_LINE = re.compile(
    r"^(\S+)-(\S+)\s+<\s+(\S+)\s*$"
)


async def _run(
    target: PatchTarget,
    cmd: str,
    *,
    timeout: float,
    connect_timeout: float,
) -> tuple[str, str, int]:
    return await ssh_run(
        target.ip,
        cmd,
        timeout=timeout,
        username=target.ssh_user,
        port=target.port,
        connect_timeout=connect_timeout,
    )


async def scan_apt(
    target: PatchTarget,
    *,
    timeout: float,
    connect_timeout: float,
) -> list[dict[str, Any]]:
    # Update indexes then list upgradable (noninteractive)
    cmd = (
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update -qq 2>/dev/null || true; "
        "apt list --upgradable 2>/dev/null"
    )
    stdout, stderr, code = await _run(
        target, cmd, timeout=timeout, connect_timeout=connect_timeout
    )
    # apt list returns 0 even when empty; non-zero can still have useful lines
    packages: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("listing"):
            continue
        m = _APT_LINE.match(line)
        if not m:
            continue
        name, archive, candidate, current = m.groups()
        priority = classify_package(name, archive=archive, repo=archive)
        packages.append(
            {
                "name": name,
                "current": current,
                "candidate": candidate,
                "priority": priority,
                "meta": {"archive": archive, "raw": line},
            }
        )
    if not packages and code not in (0, 100) and not stdout.strip():
        raise ScanError((stderr or stdout or f"apt exit {code}").strip())
    return packages


async def scan_dnf(
    target: PatchTarget,
    *,
    timeout: float,
    connect_timeout: float,
    pm: str = "dnf",
) -> list[dict[str, Any]]:
    # check-update: exit 100 = updates available, 0 = none
    bin_name = "dnf" if pm == "dnf" else "yum"
    cmd = f"{bin_name} -q check-update 2>/dev/null || true"
    stdout, stderr, code = await _run(
        target, cmd, timeout=timeout, connect_timeout=connect_timeout
    )
    packages: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Obsoleting") or line.startswith("Security:"):
            continue
        # skip headers / blank separators
        parts = line.split()
        if len(parts) < 3:
            continue
        name_arch, version, repo = parts[0], parts[1], parts[2]
        name = name_arch.rsplit(".", 1)[0] if "." in name_arch else name_arch
        priority = classify_package(name, repo=repo)
        packages.append(
            {
                "name": name,
                "current": None,
                "candidate": version,
                "priority": priority,
                "meta": {"repo": repo, "arch_pkg": name_arch, "raw": line},
            }
        )

    # Enrich security via updateinfo when available
    info_cmd = f"{bin_name} -q updateinfo list security 2>/dev/null || true"
    info_out, _, _ = await _run(
        target, info_cmd, timeout=min(60.0, timeout), connect_timeout=connect_timeout
    )
    sec_names: set[str] = set()
    for line in info_out.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            # typical: FEDORA-… Important/Sec. package-version
            pkg = parts[-1]
            base = pkg.rsplit("-", 2)[0] if "-" in pkg else pkg
            sec_names.add(base.split(".")[0])
            for p in packages:
                if p["name"] == base or p["name"].startswith(base + "-"):
                    p["priority"] = "security"
                    sec_names.add(p["name"])

    for p in packages:
        if any(p["name"] == s or p["name"].startswith(s) for s in sec_names):
            p["priority"] = "security"

    return packages


async def scan_apk(
    target: PatchTarget,
    *,
    timeout: float,
    connect_timeout: float,
) -> list[dict[str, Any]]:
    cmd = "apk update -q 2>/dev/null || true; apk version -l '<' 2>/dev/null || true"
    stdout, stderr, code = await _run(
        target, cmd, timeout=timeout, connect_timeout=connect_timeout
    )
    packages: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _APK_LINE.match(line)
        if m:
            name, current, candidate = m.groups()
        else:
            # fallback: name-ver < newver
            if "<" not in line:
                continue
            left, _, right = line.partition("<")
            left, right = left.strip(), right.strip()
            # strip trailing version from left: pkg-1.2.3
            name = left.rsplit("-", 1)[0] if "-" in left else left
            current = left[len(name) + 1 :] if left.startswith(name + "-") else None
            candidate = right
        priority = classify_package(name)
        packages.append(
            {
                "name": name,
                "current": current,
                "candidate": candidate,
                "priority": priority,
                "meta": {"raw": line},
            }
        )
    if not packages and code not in (0,) and not stdout.strip():
        raise ScanError((stderr or f"apk exit {code}").strip())
    return packages


async def _emit(
    progress: ProgressFn | None,
    phase: str,
    percent: int,
    message: str,
) -> None:
    if not progress:
        return
    result = progress(phase, percent, message)
    if asyncio.iscoroutine(result):
        await result


async def scan_target(
    target: PatchTarget,
    *,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Full scan: detect → list packages → reboot flag → summary."""
    ps = get_patcher_settings()
    timeout = ps.patcher_scan_timeout
    connect_timeout = ps.patcher_connect_timeout

    await _emit(
        progress,
        "Verbinden",
        12,
        f"SSH zu {target.name} ({target.ip})…",
    )
    detect: HostDetect = await detect_host(
        target, timeout=min(60.0, timeout), connect_timeout=connect_timeout
    )
    pm = detect.pm
    await _emit(
        progress,
        "Erkannt",
        28,
        f"{detect.pretty_name} · Paketmanager {pm}",
    )

    if pm == "apt":
        await _emit(
            progress,
            "apt update",
            40,
            "Paketquellen aktualisieren & ausstehende Updates lesen…",
        )
        packages = await scan_apt(
            target, timeout=timeout, connect_timeout=connect_timeout
        )
    elif pm in ("dnf", "yum"):
        await _emit(
            progress,
            f"{pm} check",
            40,
            "Ausstehende Updates prüfen…",
        )
        packages = await scan_dnf(
            target, timeout=timeout, connect_timeout=connect_timeout, pm=pm
        )
    elif pm == "apk":
        await _emit(
            progress,
            "apk",
            40,
            "Alpine-Pakete prüfen…",
        )
        packages = await scan_apk(
            target, timeout=timeout, connect_timeout=connect_timeout
        )
    else:
        raise ScanError(f"Nicht unterstützter Paketmanager: {pm}")

    await _emit(
        progress,
        "Auswerten",
        70,
        f"{len(packages)} Paket(e) gefunden — Reboot-Flag prüfen…",
    )
    reboot = await check_reboot_required(
        target, timeout=30.0, connect_timeout=connect_timeout
    )
    summary = summarize_packages(packages)
    summary["distro"] = detect.pretty_name
    summary["pm"] = pm
    summary["reboot_required"] = reboot

    await _emit(
        progress,
        "Speichern",
        85,
        f"{summary.get('total', 0)} Updates "
        f"({summary.get('security', 0)} Security)"
        + (" — Reboot empfohlen" if reboot else ""),
    )

    return {
        "pm": pm,
        "distro": detect.pretty_name,
        "distro_id": detect.distro,
        "packages": packages,
        "summary": summary,
        "reboot_required": reboot,
    }
