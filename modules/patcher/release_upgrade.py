"""Perform a confirmed Ubuntu release upgrade (sequential hops if needed).

Never called from daily/scan-all. One operator confirm covers the full path
to the recommended destination (e.g. 24.10 → 26.04 LTS). Intermediate
EOL series are execution hops only, not recommendations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.core.docker_control import DockerControlError
from patcher.apply import (
    ApplyError,
    _apt_cmd,
    _maybe_rewrite_ubuntu_eol,
    _rewrite_ubuntu_sources,
    _stream_apply_cmd,
    reboot_host,
)
from patcher.config import get_patcher_settings
from patcher.detect import HostDetect, check_reboot_required, detect_host
from patcher.release import (
    ReleaseHop,
    suggest_release_upgrade,
    ubuntu_series_is_eol,
)
from patcher.sshutil import ssh_probe, ssh_run
from patcher.targets import PatchTarget
from patcher.ubuntu_eol import parse_ubuntu_version

logger = logging.getLogger(__name__)

ProgressFn = Any
LogFn = Any


def _guest_kind_label(target: PatchTarget, detect: HostDetect) -> str:
    kind = (target.kind or "").lower()
    if kind == "lxc" or detect.virt in ("lxc", "lxc-libvirt"):
        return "LXC"
    if kind == "qemu":
        return "VM (QEMU)"
    if detect.container or detect.virt in ("docker", "podman", "container", "openvz"):
        return f"Container ({detect.virt})"
    if detect.virt and detect.virt not in ("unknown", "none"):
        return detect.virt
    if kind == "manual":
        return "Host"
    return kind or "Host"


def _set_prompt_cmd(prompt: str) -> str:
    value = "lts" if prompt == "lts" else "normal"
    return f"""
export DEBIAN_FRONTEND=noninteractive LC_ALL=C
mkdir -p /etc/update-manager
if [ -f /etc/update-manager/release-upgrades ]; then
  if grep -qE '^Prompt=' /etc/update-manager/release-upgrades; then
    sed -i -E 's/^Prompt=.*/Prompt={value}/' /etc/update-manager/release-upgrades
  else
    printf '\\nPrompt={value}\\n' >> /etc/update-manager/release-upgrades
  fi
else
  printf '[DEFAULT]\\nPrompt={value}\\n' > /etc/update-manager/release-upgrades
fi
echo PROMPT_SET={value}
"""


def _release_upgrade_cmd(*, container: bool) -> str:
    """Non-interactive do-release-upgrade; LXC skips snap where possible."""
    snap_skip = ""
    if container:
        snap_skip = (
            "if command -v snap >/dev/null 2>&1; then "
            "snap set system refresh.hold='2999-01-01T00:00:00Z' >/dev/null 2>&1 || true; "
            "fi; "
        )
    return (
        "export DEBIAN_FRONTEND=noninteractive LC_ALL=C "
        "NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1 "
        "UCF_FORCE_CONFFOLD=1 APT_LISTCHANGES_FRONTEND=none "
        "RELEASE_UPGRADER_ALLOW_THIRD_PARTY=1; "
        "apt-get install -y update-manager-core "
        "-o DPkg::Lock::Timeout=60 "
        "-o Acquire::ForceIPv4=true; "
        f"{snap_skip}"
        "if ! command -v do-release-upgrade >/dev/null 2>&1; then "
        "echo 'do-release-upgrade fehlt (update-manager-core)'; exit 2; fi; "
        "do-release-upgrade -f DistUpgradeViewNonInteractive -m server"
    )


async def _emit(progress: ProgressFn | None, phase: str, percent: int, message: str) -> None:
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


async def _wait_ssh(
    target: PatchTarget,
    *,
    timeout: float,
    connect_timeout: float,
    on_log: LogFn | None,
) -> None:
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            result = await ssh_probe(
                target.ip,
                username=target.ssh_user,
                port=target.port,
                connect_timeout=min(12.0, connect_timeout),
            )
            if result.get("ok"):
                await _emit_log(on_log, "SSH wieder erreichbar.")
                return
        except DockerControlError:
            pass
        await _emit_log(on_log, f"Warte auf SSH (Versuch {attempt})…")
        await asyncio.sleep(8)
    raise ApplyError(
        f"Host {target.name} nach dem Neustart nicht mehr per SSH erreichbar "
        f"(Timeout {int(timeout)}s)."
    )


async def perform_release_upgrade(
    target: PatchTarget,
    *,
    reboot_after: bool = False,
    progress: ProgressFn | None = None,
    on_log: LogFn | None = None,
) -> dict[str, Any]:
    """Run sequential hops to the recommended destination. Caller must confirm."""
    ps = get_patcher_settings()
    hop_timeout = ps.patcher_release_timeout
    connect_timeout = ps.patcher_connect_timeout
    apply_timeout = min(ps.patcher_apply_timeout, hop_timeout)

    await _emit(progress, "Verbinden", 4, f"SSH zu {target.name} ({target.ip})…")
    detect = await detect_host(
        target, timeout=min(60.0, apply_timeout), connect_timeout=connect_timeout
    )
    if detect.pm != "apt":
        raise ApplyError(
            "Release-Upgrade ist nur für Ubuntu (apt) umgesetzt "
            f"(erkannt: {detect.pretty_name}, {detect.pm})."
        )

    suggestion = suggest_release_upgrade(
        distro=detect.distro,
        version_id=detect.version_id,
        pretty_name=detect.pretty_name,
        codename=detect.version_codename,
    )
    if suggestion is None or not suggestion.available:
        raise ApplyError(
            f"Kein Release-Upgrade-Pfad für {detect.pretty_name}."
        )
    if not suggestion.performable or suggestion.method != "do-release-upgrade":
        raise ApplyError(
            suggestion.reason
            or "Dieses Release-Upgrade wird nicht automatisch ausgeführt."
        )

    guest = _guest_kind_label(target, detect)
    hops = suggestion.hops
    log_parts: list[str] = []

    await _emit(
        progress,
        "Pfad",
        8,
        f"{suggestion.headline} · {guest}",
    )
    await _emit_log(
        on_log,
        f"{suggestion.headline} ({guest}). {suggestion.path_label}",
    )
    await _emit_log(on_log, suggestion.reason)
    if detect.container or guest == "LXC":
        await _emit_log(
            on_log,
            "Container/LXC erkannt: NEEDRESTART wird unterdrückt, Snap-Refresh "
            "zurückgehalten. Overlay/fehlende Snaps können das Upgrade stören.",
        )

    await _maybe_rewrite_ubuntu_eol(
        target,
        detect,
        timeout=min(90.0, apply_timeout),
        connect_timeout=connect_timeout,
        progress=progress,
        on_log=on_log,
    )

    await _emit(
        progress,
        "Vorbereitung",
        12,
        "Aktuelle Pakete aktualisieren (Voraussetzung für do-release-upgrade)…",
    )
    await _emit_log(on_log, "apt-get update + upgrade auf der aktuellen Version…")
    stdout, stderr, code = await _stream_apply_cmd(
        target,
        _apt_cmd("all", []),
        timeout=apply_timeout,
        connect_timeout=connect_timeout,
        progress=progress,
        on_log=on_log,
    )
    blob = ((stdout or "") + ("\n" + stderr if stderr else "")).strip()
    if blob:
        log_parts.append(blob)
    if code != 0:
        raise ApplyError(
            "Vorbereitung (apt upgrade) fehlgeschlagen — Release-Upgrade "
            "nicht gestartet: " + (stderr or stdout or f"exit {code}")[:700]
        )

    container = bool(detect.container or guest == "LXC")
    hop_n = len(hops)
    last_detect = detect

    for idx, hop in enumerate(hops, start=1):
        base = 18 + int(((idx - 1) / hop_n) * 70)
        await _emit(
            progress,
            f"Schritt {idx}/{hop_n}",
            base,
            hop.label or f"{hop.source} → {hop.target}",
        )
        await _emit_log(
            on_log,
            f"=== {hop.label} (Prompt={hop.prompt}) ===",
        )

        last_detect = await _run_one_hop(
            target,
            hop,
            container=container,
            timeout=hop_timeout,
            connect_timeout=connect_timeout,
            progress=progress,
            on_log=on_log,
            log_parts=log_parts,
            percent=base + 8,
        )

        more = idx < hop_n
        reboot_needed = await check_reboot_required(
            target, timeout=30.0, connect_timeout=connect_timeout
        )
        if more and reboot_needed:
            await _emit(
                progress,
                "Zwischen-Reboot",
                min(88, base + 16),
                "Neustart nötig, damit der nächste Schritt starten kann…",
            )
            await _emit_log(
                on_log,
                "Zwischen-Neustart (erforderlich für den nächsten do-release-upgrade-Schritt).",
            )
            rb = await reboot_host(target, confirm=True)
            await _emit_log(on_log, rb.get("message") or "Reboot geplant.")
            await asyncio.sleep(8)
            await _wait_ssh(
                target,
                timeout=min(600.0, hop_timeout),
                connect_timeout=connect_timeout,
                on_log=on_log,
            )
            last_detect = await detect_host(
                target, timeout=60.0, connect_timeout=connect_timeout
            )
            await _emit_log(on_log, f"Nach Reboot: {last_detect.pretty_name}")
        if more:
            await _prepare_next_hop_packages(
                target,
                detect=last_detect,
                timeout=apply_timeout,
                connect_timeout=connect_timeout,
                progress=progress,
                on_log=on_log,
                log_parts=log_parts,
                percent=min(88, base + 18),
            )

    final = last_detect
    try:
        final = await detect_host(
            target, timeout=60.0, connect_timeout=connect_timeout
        )
    except Exception as exc:
        await _emit_log(on_log, f"Erneutes Detect übersprungen: {exc}")

    reached = parse_ubuntu_version(final.version_id, final.pretty_name)
    expected = suggestion.target_version
    if reached and reached != expected:
        raise ApplyError(
            f"Upgrade unvollständig: Host ist jetzt {final.pretty_name} "
            f"(erwartet {suggestion.target_pretty}). "
            "Bitte Logs prüfen und nach einem Neustart erneut scannen."
        )

    reboot = await check_reboot_required(
        target, timeout=30.0, connect_timeout=connect_timeout
    )
    reboot_scheduled = False
    reboot_error: str | None = None
    done_msg = f"Release-Upgrade fertig: {final.pretty_name}."
    if reboot_after:
        await _emit(progress, "Reboot", 94, f"Starte {target.name} neu…")
        try:
            rb = await reboot_host(target, confirm=True)
            reboot_scheduled = True
            done_msg = f"{done_msg} Reboot wurde geplant."
            await _emit_log(on_log, rb.get("message") or "Reboot geplant.")
        except ApplyError as exc:
            reboot_error = exc.message
            done_msg = f"{done_msg} Reboot fehlgeschlagen: {exc.message}"
            await _emit_log(on_log, done_msg)
    elif reboot:
        done_msg += " Reboot empfohlen — bitte manuell bestätigen."

    await _emit(progress, "Abschluss", 96, done_msg)
    return {
        "pm": final.pm,
        "distro": final.pretty_name,
        "version_id": final.version_id,
        "virt": final.virt,
        "container": bool(final.container),
        "guest_kind": guest,
        "release_upgrade": suggestion.to_dict(),
        "log": "\n".join(log_parts)[-12000:],
        "reboot_required": reboot,
        "reboot_after": reboot_after,
        "reboot_scheduled": reboot_scheduled,
        "reboot_error": reboot_error,
        "exit_code": 0,
    }


async def _prepare_next_hop_packages(
    target: PatchTarget,
    *,
    detect: HostDetect,
    timeout: float,
    connect_timeout: float,
    progress: ProgressFn | None,
    on_log: LogFn | None,
    log_parts: list[str],
    percent: int,
) -> None:
    """EOL sources + apt upgrade so the next do-release-upgrade can start."""
    ver = parse_ubuntu_version(detect.version_id, detect.pretty_name)
    if ubuntu_series_is_eol(ver):
        await _emit_log(
            on_log,
            "Zwischenstand ist ebenfalls EOL — Quellen auf old-releases.",
        )
        await _rewrite_ubuntu_sources(
            target,
            timeout=min(90.0, timeout),
            connect_timeout=connect_timeout,
            on_log=on_log,
        )
    await _emit(
        progress,
        "Zwischen-Update",
        percent,
        "Pakete der Zwischenversion aktualisieren…",
    )
    stdout, stderr, code = await _stream_apply_cmd(
        target,
        _apt_cmd("all", []),
        timeout=timeout,
        connect_timeout=connect_timeout,
        progress=progress,
        on_log=on_log,
    )
    blob = ((stdout or "") + ("\n" + stderr if stderr else "")).strip()
    if blob:
        log_parts.append(blob)
    if code != 0:
        raise ApplyError(
            "Zwischen-Update fehlgeschlagen — nächster Release-Schritt "
            "nicht gestartet: " + (stderr or stdout or f"exit {code}")[:700]
        )


async def _run_one_hop(
    target: PatchTarget,
    hop: ReleaseHop,
    *,
    container: bool,
    timeout: float,
    connect_timeout: float,
    progress: ProgressFn | None,
    on_log: LogFn | None,
    log_parts: list[str],
    percent: int,
) -> HostDetect:
    stdout, stderr, code = await ssh_run(
        target.ip,
        _set_prompt_cmd(hop.prompt),
        timeout=45.0,
        username=target.ssh_user,
        port=target.port,
        connect_timeout=connect_timeout,
    )
    blob = ((stdout or "") + ("\n" + stderr if stderr else "")).strip()
    if blob:
        await _emit_log(on_log, blob.splitlines()[-1][:200])
    if code != 0:
        raise ApplyError(
            f"Prompt={hop.prompt} in /etc/update-manager/release-upgrades "
            f"fehlgeschlagen: {(stderr or stdout or '')[:400]}"
        )

    await _emit(
        progress,
        hop.label or "Upgrade",
        percent,
        f"do-release-upgrade {hop.source} → {hop.target} "
        f"(kann 30–90+ Minuten dauern)…",
    )
    stdout, stderr, code = await _stream_apply_cmd(
        target,
        _release_upgrade_cmd(container=container),
        timeout=timeout,
        connect_timeout=connect_timeout,
        progress=progress,
        on_log=on_log,
    )
    blob = ((stdout or "") + ("\n" + stderr if stderr else "")).strip()
    if blob:
        log_parts.append(blob)

    low = blob.lower()
    if "no new release found" in low or "kein neues release" in low:
        raise ApplyError(
            f"Kein Release von {hop.source} nach {hop.target} gefunden. "
            "Quellen (old-releases) und update-manager-core prüfen."
        )
    if code != 0:
        raise ApplyError(
            f"do-release-upgrade fehlgeschlagen ({hop.label}, exit {code}): "
            + (stderr or stdout or "")[:800]
        )

    detect = await detect_host(
        target, timeout=60.0, connect_timeout=connect_timeout
    )
    now = parse_ubuntu_version(detect.version_id, detect.pretty_name)
    if now and now != hop.target:
        # Some upgrades reboot before os-release updates; still flag mismatch.
        await _emit_log(
            on_log,
            f"Hinweis: erkannt {detect.pretty_name} (Schrittziel {hop.target}).",
        )
        if now == hop.source:
            raise ApplyError(
                f"Version unverändert ({detect.pretty_name}). "
                "do-release-upgrade hat das Release nicht gewechselt."
            )
    return detect
