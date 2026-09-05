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
    build_meta_release_pin,
    hop_failure_message,
    should_use_devel_flag,
    suggest_release_upgrade,
    ubuntu_codename,
    ubuntu_series_is_eol,
    upgrade_tool_url_candidates,
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


_HLOPS_UPGRADER_DIR = "/var/tmp/ubuntu-release-upgrader"
_HLOPS_APT_SANDBOX = "/etc/apt/apt.conf.d/zz-hlops-release-upgrade"
_HLOPS_META = "/etc/update-manager/meta-release"
_HLOPS_META_BAK = "/etc/update-manager/meta-release.hlops-bak"


def _release_upgrade_cmd(hop: ReleaseHop, *, container: bool) -> str:
    """Pin this hop's DistUpgrade tarball; LXC-safe fetch; never ``-d`` unless devel.

    Official meta-release marks EOL interims Supported: 0, so do-release-upgrade
    skips 25.04/25.10 and fetches resolute.tar.gz. We write a two-dist pin and
    invoke that hop's tarball directly.
    """
    code = hop.target_codename or ubuntu_codename(hop.target)
    use_d = should_use_devel_flag(hop)
    meta = build_meta_release_pin(source=hop.source, target=hop.target)
    urls = upgrade_tool_url_candidates(hop.target)
    url_list = " ".join(urls)
    snap_skip = ""
    if container:
        snap_skip = (
            "if command -v snap >/dev/null 2>&1; then "
            "snap set system refresh.hold='2999-01-01T00:00:00Z' >/dev/null 2>&1 || true; "
            "fi\n"
        )
    if use_d:
        invoke = (
            "echo HLOPS_HOP_TARGET=devel\n"
            "do-release-upgrade -d -f DistUpgradeViewNonInteractive -m server\n"
        )
        fetch = ""
    else:
        fetch = f"""
CODE={code}
WORKDIR={_HLOPS_UPGRADER_DIR}
echo "Schrittziel $CODE — DistUpgrade-Tarball (kein -d)"
FOUND=""
for url in {url_list}; do
  echo "Versuche DistUpgrade-Tarball: $url"
  if command -v curl >/dev/null 2>&1; then
    if curl -fsSL --connect-timeout 20 -o "$WORKDIR/$CODE.tar.gz" "$url"; then
      curl -fsSL --connect-timeout 20 -o "$WORKDIR/$CODE.tar.gz.gpg" "$url.gpg" || true
      FOUND=$url
      echo "DistUpgrade-Tarball gefunden: $url"
      break
    fi
    echo "DistUpgrade-Tarball nicht unter $url — nächste Quelle."
  elif command -v wget >/dev/null 2>&1; then
    if wget -q -O "$WORKDIR/$CODE.tar.gz" "$url"; then
      wget -q -O "$WORKDIR/$CODE.tar.gz.gpg" "$url.gpg" || true
      FOUND=$url
      echo "DistUpgrade-Tarball gefunden: $url"
      break
    fi
    echo "DistUpgrade-Tarball nicht unter $url — nächste Quelle."
  else
    echo "weder curl noch wget vorhanden"; exit 2
  fi
done
if [ -z "$FOUND" ] || [ ! -s "$WORKDIR/$CODE.tar.gz" ]; then
  echo "DistUpgrade-Tarball für $CODE nicht gefunden (old-releases/archive, dists/$CODE/)."
  exit 1
fi
echo "DistUpgrade geladen von: $FOUND"
chmod 0644 "$WORKDIR/$CODE.tar.gz"
[ -f "$WORKDIR/$CODE.tar.gz.gpg" ] && chmod 0644 "$WORKDIR/$CODE.tar.gz.gpg" || true
if command -v gpgv >/dev/null 2>&1 && [ -s "$WORKDIR/$CODE.tar.gz.gpg" ]; then
  echo "gpgv: prüfe $CODE.tar.gz"
  KR=""
  for k in /usr/share/keyrings/ubuntu-archive-keyring.gpg \\
           /usr/share/keyrings/ubuntu-archive-removed-keys.gpg \\
           /etc/apt/trusted.gpg \\
           /etc/apt/trusted.gpg.d/ubuntu-keyring-2012-archive.gpg \\
           /etc/apt/trusted.gpg.d/ubuntu-keyring-2018-archive.gpg \\
           /etc/apt/trusted.gpg.d/*.gpg; do
    [ -s "$k" ] || continue
    KR="$KR --keyring $k"
  done
  if [ -n "$KR" ]; then
    gpgv $KR "$WORKDIR/$CODE.tar.gz.gpg" "$WORKDIR/$CODE.tar.gz" \\
      || echo "Hinweis: gpgv-Prüfung fehlgeschlagen — fahre fort."
  fi
else
  echo "Hinweis: keine gpgv-Signaturprüfung (gpgv oder .gpg fehlt)."
fi
EXTRACT="$WORKDIR/$CODE.d"
echo "Entpacke nach $EXTRACT (--no-same-owner, nicht ins Arbeitsverzeichnis)"
rm -rf "$EXTRACT"
mkdir -p "$EXTRACT"
chmod 0755 "$EXTRACT"
if ! tar --no-same-owner -xzf "$WORKDIR/$CODE.tar.gz" -C "$EXTRACT"; then
  echo "tar --no-same-owner fehlgeschlagen — versuche tar -xzf"
  tar -xzf "$WORKDIR/$CODE.tar.gz" -C "$EXTRACT" || {{
    echo "DistUpgrade-Tarball konnte nicht entpackt werden"; exit 2
  }}
fi
SCRIPT=""
if [ -f "$EXTRACT/$CODE" ]; then SCRIPT="$EXTRACT/$CODE"
elif [ -f "$EXTRACT/dist-upgrade.py" ]; then SCRIPT="$EXTRACT/dist-upgrade.py"
else SCRIPT=$(find "$EXTRACT" -maxdepth 3 -type f -name "$CODE" | head -n 1)
fi
if [ -z "$SCRIPT" ] || [ ! -f "$SCRIPT" ]; then
  echo "DistUpgrade-Skript $CODE fehlt nach dem Entpacken in $EXTRACT"
  ls -la "$EXTRACT" | head -n 20
  exit 2
fi
chmod a+x "$SCRIPT" || true
if [ ! -x "$SCRIPT" ]; then
  echo "DistUpgrade-Skript $SCRIPT ist nicht ausführbar"; exit 2
fi
echo "DistUpgrade-Skript: $SCRIPT"
echo HLOPS_HOP_TARGET=$CODE
mkdir -p /var/log/dist-upgrade /var/run
# Spare sshd on :1022 hangs or is missing in LXC; we always come in via SSH.
: > /var/run/release-upgrader-sshd.pid
export RELEASE_UPGRADER_NO_SCREEN=1 PYTHONUNBUFFERED=1
echo "Starte DistUpgrade ./$CODE --frontend=DistUpgradeViewNonInteractive --datadir=$EXTRACT"
cd "$(dirname "$SCRIPT")"
set +e
./"$(basename "$SCRIPT")" --frontend=DistUpgradeViewNonInteractive --mode=server --datadir=. --disable-gnu-screen
UP_RC=$?
echo "DistUpgrade Exit: $UP_RC"
if [ "$UP_RC" != 0 ]; then
  echo "----- letzte 30 Zeilen /var/log/dist-upgrade/main.log -----"
  if [ -f /var/log/dist-upgrade/main.log ]; then
    tail -n 30 /var/log/dist-upgrade/main.log
  else
    echo "(kein /var/log/dist-upgrade/main.log — Abbruch vor DistUpgrade-Logging)"
  fi
  if [ -f /var/log/dist-upgrade/apt.log ]; then
    echo "----- letzte 20 Zeilen /var/log/dist-upgrade/apt.log -----"
    tail -n 20 /var/log/dist-upgrade/apt.log
  fi
  exit "$UP_RC"
fi
"""
        invoke = ""

    return f"""
export DEBIAN_FRONTEND=noninteractive LC_ALL=C \\
  DEBIAN_PRIORITY=critical DEBCONF_NONINTERACTIVE_SEEN=true \\
  NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1 \\
  UCF_FORCE_CONFFOLD=1 APT_LISTCHANGES_FRONTEND=none \\
  RELEASE_UPGRADER_ALLOW_THIRD_PARTY=1 \\
  RELEASE_UPGRADER_NO_SCREEN=1 PYTHONUNBUFFERED=1 \\
  TMPDIR={_HLOPS_UPGRADER_DIR}
WORKDIR={_HLOPS_UPGRADER_DIR}
HL_APT_CONF={_HLOPS_APT_SANDBOX}
HL_META={_HLOPS_META}
HL_META_BAK={_HLOPS_META_BAK}
mkdir -p "$WORKDIR" /etc/update-manager /etc/apt/apt.conf.d
chmod 0755 "$WORKDIR"
# Leftover pin from a crashed hop
if [ -f "$HL_META_BAK" ]; then mv -f "$HL_META_BAK" "$HL_META" || true; fi
rm -f "$HL_APT_CONF"
cleanup_hlops() {{
  rm -f "$HL_APT_CONF"
  if [ -f "$HL_META_BAK" ]; then mv -f "$HL_META_BAK" "$HL_META" || true; fi
}}
trap cleanup_hlops EXIT
# Scoped to this command only (LXC: _apt cannot enter mkdtemp 0700 dirs)
printf 'APT::Sandbox::User "root";\\n' > "$HL_APT_CONF"
if [ -f "$HL_META" ]; then cp -a "$HL_META" "$HL_META_BAK"; fi
cat > "$WORKDIR/meta-release" <<'HLOPS_META_EOF'
{meta}
HLOPS_META_EOF
chmod 0644 "$WORKDIR/meta-release"
cat > "$HL_META" <<'HLOPS_URI_EOF'
[METARELEASE]
URI = file://{_HLOPS_UPGRADER_DIR}/meta-release
URI_LTS = file://{_HLOPS_UPGRADER_DIR}/meta-release
URI_UNSTABLE_POSTFIX =
URI_PROPOSED_POSTFIX =
HLOPS_URI_EOF
echo "meta-release gepinnt: {hop.source} → {hop.target} ({code})"
apt-get install -y update-manager-core gpgv \\
  -o DPkg::Lock::Timeout=60 \\
  -o Acquire::ForceIPv4=true \\
  -o APT::Sandbox::User=root
{snap_skip}if ! command -v do-release-upgrade >/dev/null 2>&1; then
  echo 'do-release-upgrade fehlt (update-manager-core)'; exit 2
fi
{fetch}{invoke}
"""


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
            "Zwischenstand ist ebenfalls EOL — Quellen auf "
            "old-releases.ubuntu.com (nicht archive.ubuntu.com).",
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
        f"({hop.target_codename or hop.target}, ohne -d, kann 30–90+ Minuten dauern)…",
    )
    await _emit_log(
        on_log,
        f"Schrittziel {hop.target} ({hop.target_codename}) — meta-release "
        "gepinnt, DistUpgrade-Tarball für genau dieses Release, kein -d.",
    )
    stdout, stderr, code = await _stream_apply_cmd(
        target,
        _release_upgrade_cmd(hop, container=container),
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
            "Quellen (old-releases) und update-manager-core prüfen. "
            "Ein Proxmox-Snapshot (hlops-*) vor dem Versuch kann zur Rückkehr "
            "genutzt werden."
        )
    if code != 0:
        raise ApplyError(hop_failure_message(hop, code, blob or stderr or stdout or ""))

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
