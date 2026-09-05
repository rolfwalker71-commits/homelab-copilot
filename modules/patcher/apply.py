"""Apply package updates over SSH (apt / dnf / yum / apk)."""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from collections.abc import Awaitable, Callable
from typing import Any

from patcher.config import get_patcher_settings
from patcher.detect import HostDetect, check_reboot_required, detect_host
from patcher.sshutil import ssh_run, ssh_run_stream
from patcher.targets import PatchTarget
from patcher.ubuntu_eol import (
    rewrite_ubuntu_sources_cmd,
    should_rewrite_after_apt_error,
    ubuntu_eol_reason,
)

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, str], Awaitable[None] | None]
LogFn = Callable[[str], Awaitable[None] | None]


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


async def _emit_log(on_log: LogFn | None, line: str) -> None:
    if not on_log:
        return
    result = on_log(line)
    if asyncio.iscoroutine(result):
        await result


def _line_buffered(cmd: str) -> str:
    quoted = shlex.quote(cmd)
    return (
        "if command -v stdbuf >/dev/null 2>&1; then "
        f"stdbuf -oL -eL sh -c {quoted}; "
        f"else {cmd}; fi"
    )


async def _maybe_rewrite_ubuntu_eol(
    target: PatchTarget,
    detect: HostDetect,
    *,
    timeout: float,
    connect_timeout: float,
    progress: ProgressFn | None,
    on_log: LogFn | None,
) -> bool:
    reason = ubuntu_eol_reason(
        distro=detect.distro,
        version_id=detect.version_id,
        pretty_name=detect.pretty_name,
    )
    if not reason:
        return False
    await _emit(progress, "EOL", 20, reason)
    await _emit_log(on_log, reason)
    return await _rewrite_ubuntu_sources(
        target,
        timeout=timeout,
        connect_timeout=connect_timeout,
        on_log=on_log,
    )


async def _rewrite_ubuntu_sources(
    target: PatchTarget,
    *,
    timeout: float,
    connect_timeout: float,
    on_log: LogFn | None,
) -> bool:
    await _emit_log(
        on_log,
        "Schreibe apt-Quellen um: archive.ubuntu.com / security.ubuntu.com "
        "→ old-releases.ubuntu.com (Backup *.bak-eol)…",
    )
    stdout, stderr, code = await ssh_run(
        target.ip,
        rewrite_ubuntu_sources_cmd(),
        timeout=timeout,
        username=target.ssh_user,
        port=target.port,
        connect_timeout=connect_timeout,
    )
    blob = ((stdout or "") + ("\n" + stderr if stderr else "")).strip()
    for line in blob.splitlines():
        if line.strip():
            await _emit_log(on_log, line.strip())
    if code != 0:
        raise ApplyError(
            "Umstellen der Paketquellen fehlgeschlagen: "
            + (stderr or stdout or f"exit {code}")[:600]
        )
    rewritten = "EOL_REWRITE=1" in blob
    if rewritten:
        await _emit_log(
            on_log,
            "Paketquellen umgestellt. apt-get update nutzt jetzt old-releases.",
        )
    return rewritten


async def _stream_apply_cmd(
    target: PatchTarget,
    cmd: str,
    *,
    timeout: float,
    connect_timeout: float,
    progress: ProgressFn | None,
    on_log: LogFn | None,
) -> tuple[str, str, int]:
    line_n = 0
    stop_beat = asyncio.Event()
    started = time.monotonic()

    async def handle_line(line: str) -> None:
        nonlocal line_n
        line_n += 1
        await _emit_log(on_log, line)
        if line_n == 1 or line_n % 3 == 0:
            await _emit(
                progress,
                "Einspielen",
                min(82, 28 + min(50, line_n)),
                line[:200],
            )

    async def beat() -> None:
        while not stop_beat.is_set():
            try:
                await asyncio.wait_for(stop_beat.wait(), timeout=10.0)
                return
            except asyncio.TimeoutError:
                elapsed = int(time.monotonic() - started)
                if line_n:
                    continue
                await _emit(
                    progress,
                    "Einspielen",
                    min(80, 25 + elapsed // 8),
                    f"Installation läuft seit {elapsed}s — bitte warten…",
                )

    beat_task = asyncio.create_task(beat())
    try:
        return await ssh_run_stream(
            target.ip,
            _line_buffered(cmd),
            timeout=timeout,
            username=target.ssh_user,
            port=target.port,
            connect_timeout=connect_timeout,
            on_line=handle_line,
        )
    finally:
        stop_beat.set()
        beat_task.cancel()
        try:
            await beat_task
        except asyncio.CancelledError:
            pass


async def apply_updates(
    target: PatchTarget,
    *,
    package_filter: str = "all",
    packages: list[str] | None = None,
    reboot_after: bool = False,
    progress: ProgressFn | None = None,
    on_log: LogFn | None = None,
) -> dict[str, Any]:
    """Install updates. package_filter: security | all | selected."""
    ps = get_patcher_settings()
    timeout = ps.patcher_apply_timeout
    connect_timeout = ps.patcher_connect_timeout

    await _emit(
        progress,
        "Verbinden",
        8,
        f"SSH zu {target.name} ({target.ip})…",
    )
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

    await _emit(
        progress,
        "Erkannt",
        18,
        f"{detect.pretty_name} · Paketmanager {pm}",
    )

    eol_rewritten = False
    if pm == "apt":
        eol_rewritten = await _maybe_rewrite_ubuntu_eol(
            target,
            detect,
            timeout=min(90.0, timeout),
            connect_timeout=connect_timeout,
            progress=progress,
            on_log=on_log,
        )

    await _emit_log(on_log, f"Installiere Updates ({filt}) auf {target.name}…")
    await _emit(
        progress,
        "Einspielen",
        25,
        f"Updates werden installiert ({filt}) — das kann einige Minuten dauern…",
    )

    stdout, stderr, code = await _stream_apply_cmd(
        target,
        cmd,
        timeout=timeout,
        connect_timeout=connect_timeout,
        progress=progress,
        on_log=on_log,
    )
    log = ((stdout or "") + ("\n" + stderr if stderr else "")).strip()

    if (
        code != 0
        and pm == "apt"
        and not eol_rewritten
        and should_rewrite_after_apt_error(
            distro=detect.distro,
            version_id=detect.version_id,
            pretty_name=detect.pretty_name,
            apt_output=log,
        )
    ):
        await _emit_log(
            on_log,
            "apt-get update deutet auf eine abgelaufene Ubuntu-Version hin — "
            "stelle Paketquellen auf old-releases.ubuntu.com um und wiederhole…",
        )
        eol_rewritten = await _rewrite_ubuntu_sources(
            target,
            timeout=min(90.0, timeout),
            connect_timeout=connect_timeout,
            on_log=on_log,
        )
        if eol_rewritten:
            await _emit(
                progress,
                "Einspielen",
                28,
                "Paketquellen umgestellt — wiederhole apt-get update + Upgrade…",
            )
            stdout, stderr, code = await _stream_apply_cmd(
                target,
                cmd,
                timeout=timeout,
                connect_timeout=connect_timeout,
                progress=progress,
                on_log=on_log,
            )
            log = ((stdout or "") + ("\n" + stderr if stderr else "")).strip()

    if code != 0:
        raise ApplyError(
            f"Update fehlgeschlagen (exit {code}): "
            + (stderr or stdout or "")[:800]
        )

    if pm == "apt":
        await _emit(
            progress,
            "Aufräumen",
            86,
            "Entferne nicht mehr benötigte Pakete (apt-get autoremove -y)…",
        )
        await _emit_log(on_log, "apt-get autoremove -y…")
        try:
            ar_out, ar_err, ar_code = await ssh_run(
                target.ip,
                (
                    "export DEBIAN_FRONTEND=noninteractive; "
                    "apt-get -y autoremove "
                    "-o DPkg::Lock::Timeout=60"
                ),
                timeout=min(600.0, timeout),
                username=target.ssh_user,
                port=target.port,
                connect_timeout=connect_timeout,
            )
            ar_blob = ((ar_out or "") + ("\n" + ar_err if ar_err else "")).strip()
            if ar_blob:
                log = (log + "\n" + ar_blob).strip()
            if ar_code != 0:
                await _emit_log(
                    on_log,
                    "autoremove fehlgeschlagen (Updates bleiben gültig): "
                    + (ar_err or ar_out or f"exit {ar_code}")[:400],
                )
            else:
                await _emit_log(on_log, "Nicht mehr benötigte Pakete entfernt.")
        except Exception as exc:
            await _emit_log(
                on_log,
                f"autoremove übersprungen: {getattr(exc, 'message', None) or exc}",
            )

    await _emit(
        progress,
        "Reboot-Check",
        90,
        (
            "Prüfe, ob ein Neustart nötig ist…"
            if reboot_after
            else "Prüfe, ob ein Neustart nötig ist (kein automatischer Reboot)…"
        ),
    )
    reboot = await check_reboot_required(
        target, timeout=30.0, connect_timeout=connect_timeout
    )
    reboot_scheduled = False
    reboot_error: str | None = None
    done_msg = "Updates eingespielt."
    if reboot_after:
        await _emit(progress, "Reboot", 93, f"Starte {target.name} neu…")
        await _emit_log(on_log, f"Reboot nach Einspielen: {target.name}")
        try:
            rb = await reboot_host(target, confirm=True)
            reboot_scheduled = True
            done_msg = "Updates eingespielt. Reboot wurde geplant."
            await _emit_log(on_log, rb.get("message") or "Reboot geplant.")
        except ApplyError as exc:
            reboot_error = exc.message
            done_msg = f"Updates eingespielt. Reboot fehlgeschlagen: {exc.message}"
            await _emit_log(on_log, done_msg)
    elif reboot:
        done_msg += " Reboot empfohlen — bitte manuell bestätigen."
    await _emit(progress, "Abschluss", 95, done_msg)
    return {
        "pm": pm,
        "distro": detect.pretty_name,
        "log": log[-8000:],
        "reboot_required": reboot,
        "reboot_after": reboot_after,
        "reboot_scheduled": reboot_scheduled,
        "reboot_error": reboot_error,
        "exit_code": code,
    }


def _apt_cmd(filt: str, selected: list[str]) -> str:
    base = (
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update -qq "
        "-o Acquire::ForceIPv4=true "
        "-o Acquire::http::Timeout=20 "
        "-o Acquire::https::Timeout=20 "
        "-o DPkg::Lock::Timeout=60; "
    )
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
