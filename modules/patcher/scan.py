"""Scan pending package updates over SSH (apt / dnf / yum / apk)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.docker_control import DockerControlError
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
_APT_SIM_LINE = re.compile(
    r"^Inst\s+(\S+)\s+(?:\[([^\]]+)\]\s+)?\((\S+)",
    re.I,
)
_APK_LINE = re.compile(
    r"^(\S+)-(\S+)\s+<\s+(\S+)\s*$"
)
_LOCK_HINT = re.compile(
    r"could not get lock|unable to lock|unable to acquire|"
    r"dpkg frontend lock|lists/lock|another process",
    re.I,
)

# Probe locks without waiting. fuser/lsof are optional on slim LXCs.
_APT_LOCK_PROBE = (
    "LOCK=0; "
    "for f in /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock "
    "/var/lib/dpkg/lock /var/cache/apt/archives/lock; do "
    "[ -e \"$f\" ] || continue; "
    "if command -v fuser >/dev/null 2>&1 && fuser \"$f\" >/dev/null 2>&1; "
    "then LOCK=1; break; fi; "
    "if command -v lsof >/dev/null 2>&1 && lsof -t \"$f\" >/dev/null 2>&1; "
    "then LOCK=1; break; fi; "
    "done; echo APT_LOCK=$LOCK"
)

# Short acquire timeouts + IPv4 so a dead mirror/IPv6 cannot eat the SSH budget.
_APT_UPDATE_OPTS = (
    "-o Acquire::Retries=0 "
    "-o Acquire::http::Timeout=8 "
    "-o Acquire::https::Timeout=8 "
    "-o Acquire::ftp::Timeout=8 "
    "-o Acquire::ForceIPv4=true "
    "-o DPkg::Lock::Timeout=0"
)


def _section_after(text: str, marker: str) -> str:
    if marker not in text:
        return text
    return text.split(marker, 1)[1]


def _looks_like_lock(text: str) -> bool:
    return bool(_LOCK_HINT.search(text or ""))


def _parse_apt_output(stdout: str) -> list[dict[str, Any]]:
    packages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("listing"):
            continue
        m = _APT_LINE.match(line)
        if m:
            name, archive, candidate, current = m.groups()
            if name in seen:
                continue
            seen.add(name)
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
            continue
        m = _APT_SIM_LINE.match(line)
        if not m:
            continue
        name, current, candidate = m.groups()
        if name in seen:
            continue
        seen.add(name)
        priority = classify_package(name)
        packages.append(
            {
                "name": name,
                "current": current,
                "candidate": candidate,
                "priority": priority,
                "meta": {"raw": line, "via": "apt-get -s"},
            }
        )
    return packages


def _parse_dnf_output(stdout: str) -> list[dict[str, Any]]:
    packages: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Obsoleting") or line.startswith("Security:"):
            continue
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
    return packages


def _parse_apk_output(stdout: str) -> list[dict[str, Any]]:
    packages: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _APK_LINE.match(line)
        if m:
            name, current, candidate = m.groups()
        else:
            if "<" not in line:
                continue
            left, _, right = line.partition("<")
            left, right = left.strip(), right.strip()
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
    return packages


def _scan_meta(
    *,
    notes: list[str],
    index_refreshed: bool,
    apt_lock: bool = False,
) -> dict[str, Any]:
    note = " ".join(n for n in notes if n).strip() or None
    return {
        "index_refreshed": index_refreshed,
        "index_stale": not index_refreshed,
        "apt_lock": apt_lock,
        "note": note,
    }


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


async def _probe_apt_lock(
    target: PatchTarget,
    *,
    connect_timeout: float,
) -> bool:
    try:
        stdout, _, _ = await _run(
            target,
            _APT_LOCK_PROBE,
            timeout=12.0,
            connect_timeout=connect_timeout,
        )
    except DockerControlError:
        return False
    return "APT_LOCK=1" in (stdout or "")


async def _timed_remote(
    target: PatchTarget,
    inner: str,
    *,
    seconds: int,
    timeout: float,
    connect_timeout: float,
    prefix: str = "",
) -> tuple[str, str, int]:
    """Run a single command under GNU/BusyBox ``timeout`` when available."""
    pre = f"{prefix}; " if prefix else ""
    cmd = (
        f"{pre}"
        f"if command -v timeout >/dev/null 2>&1; then "
        f"timeout {seconds} {inner}; "
        f"else {inner}; fi; echo __RC__$?"
    )
    return await _run(
        target, cmd, timeout=timeout, connect_timeout=connect_timeout
    )


def _remote_rc(stdout: str, fallback: int) -> int:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith("__RC__"):
            try:
                return int(line.split("__RC__", 1)[1].strip() or fallback)
            except ValueError:
                return fallback
    return fallback


async def scan_apt(
    target: PatchTarget,
    *,
    timeout: float,
    connect_timeout: float,
    index_timeout: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """List upgradable packages. Prefer cache; time-box ``apt-get update``."""
    notes: list[str] = []
    list_cmd = (
        "export DEBIAN_FRONTEND=noninteractive LC_ALL=C; "
        f"{_APT_LOCK_PROBE}; echo '===LIST==='; "
        "if command -v timeout >/dev/null 2>&1; then TO='timeout 25'; else TO=''; fi; "
        "if command -v apt >/dev/null 2>&1; then "
        "$TO apt list --upgradable 2>/dev/null; "
        "else $TO apt-get -s upgrade 2>/dev/null; fi"
    )
    list_timeout = min(60.0, timeout)
    try:
        stdout, stderr, code = await _run(
            target, list_cmd, timeout=list_timeout, connect_timeout=connect_timeout
        )
    except DockerControlError as exc:
        locked = await _probe_apt_lock(target, connect_timeout=connect_timeout)
        if locked:
            raise ScanError(
                f"APT-Sperre auf {target.ip} — unattended-upgrades oder apt-get "
                f"blockiert den Scan. Später erneut prüfen."
            ) from exc
        raise ScanError(exc.message) from exc

    apt_lock = "APT_LOCK=1" in stdout or _looks_like_lock(stderr)
    packages = _parse_apt_output(_section_after(stdout, "===LIST==="))

    if apt_lock:
        notes.append(
            "APT-Sperre aktiv (unattended-upgrades/apt) — "
            "Cache ohne Spiegel-Aktualisierung."
        )
        if not packages and code not in (0, 100) and not stdout.strip():
            raise ScanError(
                f"APT-Sperre auf {target.ip} — keine Cache-Daten lesbar."
            )
        return packages, _scan_meta(
            notes=notes, index_refreshed=False, apt_lock=True
        )

    index_refreshed = False
    remote_secs = max(10, int(index_timeout) - 2)
    try:
        u_out, u_err, _u_code = await _timed_remote(
            target,
            f"apt-get update -qq {_APT_UPDATE_OPTS}",
            seconds=remote_secs,
            timeout=index_timeout + 8.0,
            connect_timeout=connect_timeout,
            prefix="export DEBIAN_FRONTEND=noninteractive LC_ALL=C",
        )
        u_rc = _remote_rc(u_out, _u_code)
        blob = f"{u_out}\n{u_err}"
        if _looks_like_lock(blob):
            notes.append(
                "APT-Sperre während apt-get update — Cache verwendet."
            )
            apt_lock = True
        elif u_rc == 124:
            notes.append(
                "Paketquellen-Update abgebrochen (Timeout/Spiegel) — Cache verwendet."
            )
        elif u_rc != 0:
            notes.append(
                "Paketquellen-Update fehlgeschlagen — Cache verwendet."
            )
        else:
            index_refreshed = True
            relist = (
                "export DEBIAN_FRONTEND=noninteractive LC_ALL=C; "
                "if command -v apt >/dev/null 2>&1; then "
                "apt list --upgradable 2>/dev/null; "
                "else apt-get -s upgrade 2>/dev/null; fi"
            )
            r_out, _, _ = await _run(
                target,
                relist,
                timeout=min(45.0, timeout),
                connect_timeout=connect_timeout,
            )
            packages = _parse_apt_output(r_out)
    except DockerControlError as exc:
        msg = exc.message or ""
        if "Befehl-Timeout" in msg or "Timeout" in msg:
            notes.append(
                "Paketquellen-Update abgebrochen (Timeout/Spiegel) — Cache verwendet."
            )
            logger.info("apt update timeout on %s — using cache", target.ip)
        else:
            notes.append(
                "Paketquellen-Update fehlgeschlagen — Cache verwendet."
            )
            logger.info("apt update failed on %s: %s", target.ip, msg)

    if not packages and code not in (0, 100) and not stdout.strip():
        raise ScanError((stderr or stdout or f"apt exit {code}").strip())
    return packages, _scan_meta(
        notes=notes, index_refreshed=index_refreshed, apt_lock=apt_lock
    )


async def scan_dnf(
    target: PatchTarget,
    *,
    timeout: float,
    connect_timeout: float,
    index_timeout: float,
    pm: str = "dnf",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bin_name = "dnf" if pm == "dnf" else "yum"
    notes: list[str] = []
    cache_cmd = f"{bin_name} -q -C check-update 2>/dev/null || true"
    stdout, stderr, _code = await _run(
        target,
        cache_cmd,
        timeout=min(60.0, timeout),
        connect_timeout=connect_timeout,
    )
    packages = _parse_dnf_output(stdout)
    index_refreshed = False

    remote_secs = max(10, int(index_timeout) - 2)
    try:
        n_out, n_err, n_code = await _timed_remote(
            target,
            f"{bin_name} -q check-update",
            seconds=remote_secs,
            timeout=index_timeout + 8.0,
            connect_timeout=connect_timeout,
        )
        n_rc = _remote_rc(n_out, n_code)
        # dnf: 100 = updates available, 0 = none; timeout(1) uses 124
        if n_rc == 124:
            notes.append(
                "Metadaten-Update abgebrochen (Timeout) — Cache verwendet."
            )
        elif n_rc not in (0, 100):
            notes.append("Metadaten-Update fehlgeschlagen — Cache verwendet.")
        else:
            index_refreshed = True
            packages = _parse_dnf_output(n_out)
    except DockerControlError as exc:
        notes.append("Metadaten-Update abgebrochen — Cache verwendet.")
        logger.info("%s check-update failed on %s: %s", bin_name, target.ip, exc)

    info_cmd = f"{bin_name} -q -C updateinfo list security 2>/dev/null || true"
    try:
        info_out, _, _ = await _run(
            target,
            info_cmd,
            timeout=min(45.0, timeout),
            connect_timeout=connect_timeout,
        )
    except DockerControlError:
        info_out = ""
    sec_names: set[str] = set()
    for line in info_out.splitlines():
        parts = line.split()
        if len(parts) >= 3:
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

    return packages, _scan_meta(notes=notes, index_refreshed=index_refreshed)


async def scan_apk(
    target: PatchTarget,
    *,
    timeout: float,
    connect_timeout: float,
    index_timeout: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    notes: list[str] = []
    stdout, stderr, code = await _run(
        target,
        "apk version -l '<' 2>/dev/null || true",
        timeout=min(45.0, timeout),
        connect_timeout=connect_timeout,
    )
    packages = _parse_apk_output(stdout)
    index_refreshed = False

    remote_secs = max(10, int(min(30.0, index_timeout)) - 2)
    try:
        u_out, _u_err, u_code = await _timed_remote(
            target,
            "apk update -q",
            seconds=remote_secs,
            timeout=min(index_timeout, 40.0) + 8.0,
            connect_timeout=connect_timeout,
        )
        u_rc = _remote_rc(u_out, u_code)
        if u_rc == 124:
            notes.append("apk update abgebrochen (Timeout) — Cache verwendet.")
        elif u_rc != 0:
            notes.append("apk update fehlgeschlagen — Cache verwendet.")
        else:
            index_refreshed = True
            r_out, _, _ = await _run(
                target,
                "apk version -l '<' 2>/dev/null || true",
                timeout=min(45.0, timeout),
                connect_timeout=connect_timeout,
            )
            packages = _parse_apk_output(r_out)
    except DockerControlError:
        notes.append("apk update abgebrochen — Cache verwendet.")

    if not packages and code not in (0,) and not stdout.strip():
        raise ScanError((stderr or f"apk exit {code}").strip())
    return packages, _scan_meta(notes=notes, index_refreshed=index_refreshed)


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
    index_timeout = ps.patcher_index_timeout

    await _emit(
        progress,
        "Verbinden",
        12,
        f"SSH zu {target.name} ({target.ip})…",
    )
    detect: HostDetect = await detect_host(
        target, timeout=min(45.0, timeout), connect_timeout=connect_timeout
    )
    pm = detect.pm
    await _emit(
        progress,
        "Erkannt",
        28,
        f"{detect.pretty_name} · Paketmanager {pm}",
    )

    extras: dict[str, Any] = {}
    if pm == "apt":
        await _emit(
            progress,
            "Pakete",
            40,
            "Ausstehende Updates lesen (Cache, optionale kurze Spiegel-Aktualisierung)…",
        )
        packages, extras = await scan_apt(
            target,
            timeout=timeout,
            connect_timeout=connect_timeout,
            index_timeout=index_timeout,
        )
    elif pm in ("dnf", "yum"):
        await _emit(
            progress,
            f"{pm} check",
            40,
            "Ausstehende Updates prüfen…",
        )
        packages, extras = await scan_dnf(
            target,
            timeout=timeout,
            connect_timeout=connect_timeout,
            index_timeout=index_timeout,
            pm=pm,
        )
    elif pm == "apk":
        await _emit(
            progress,
            "apk",
            40,
            "Alpine-Pakete prüfen…",
        )
        packages, extras = await scan_apk(
            target,
            timeout=timeout,
            connect_timeout=connect_timeout,
            index_timeout=index_timeout,
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
    summary.update(extras)

    note = extras.get("note")
    done_msg = (
        f"{summary.get('total', 0)} Updates "
        f"({summary.get('security', 0)} Security)"
        + (" — Reboot empfohlen" if reboot else "")
    )
    if note:
        done_msg = f"{done_msg} — {note}"

    await _emit(
        progress,
        "Speichern",
        85,
        done_msg,
    )

    return {
        "pm": pm,
        "distro": detect.pretty_name,
        "distro_id": detect.distro,
        "packages": packages,
        "summary": summary,
        "reboot_required": reboot,
    }
