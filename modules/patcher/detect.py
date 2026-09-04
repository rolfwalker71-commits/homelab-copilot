"""Detect distro and package manager over SSH."""

from __future__ import annotations

import re
from dataclasses import dataclass

from patcher.sshutil import ssh_run
from patcher.targets import PatchTarget


@dataclass
class HostDetect:
    pm: str  # apt | dnf | yum | apk
    distro: str
    pretty_name: str
    raw_os_release: str = ""


async def detect_host(target: PatchTarget, *, timeout: float = 60.0, connect_timeout: float = 15.0) -> HostDetect:
    cmd = r"""
set -e
PM=""
if command -v apt-get >/dev/null 2>&1; then PM=apt
elif command -v dnf >/dev/null 2>&1; then PM=dnf
elif command -v yum >/dev/null 2>&1; then PM=yum
elif command -v apk >/dev/null 2>&1; then PM=apk
else PM=unknown
fi
echo "PM=$PM"
if [ -f /etc/os-release ]; then
  . /etc/os-release
  echo "ID=${ID:-}"
  echo "PRETTY_NAME=${PRETTY_NAME:-}"
  echo "VERSION_ID=${VERSION_ID:-}"
else
  echo "ID="
  echo "PRETTY_NAME="
  echo "VERSION_ID="
fi
"""
    stdout, stderr, code = await ssh_run(
        target.ip,
        cmd,
        timeout=timeout,
        username=target.ssh_user,
        port=target.port,
        connect_timeout=connect_timeout,
    )
    if code != 0:
        detail = (stderr or stdout or "").strip() or f"exit {code}"
        raise RuntimeError(f"Detect fehlgeschlagen: {detail}")

    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip().strip('"')

    pm = (fields.get("PM") or "unknown").lower()
    if pm == "unknown":
        raise RuntimeError(
            "Kein unterstützter Paketmanager gefunden (apt/dnf/yum/apk)."
        )
    distro = fields.get("ID") or "linux"
    pretty = fields.get("PRETTY_NAME") or distro
    return HostDetect(pm=pm, distro=distro, pretty_name=pretty, raw_os_release=stdout)


_REBOOT_CHECK = (
    "if [ -f /var/run/reboot-required ]; then echo REBOOT=1; "
    "elif command -v needs-restarting >/dev/null 2>&1 && needs-restarting -r >/dev/null 2>&1; "
    "then echo REBOOT=1; else echo REBOOT=0; fi"
)


async def check_reboot_required(
    target: PatchTarget,
    *,
    timeout: float = 30.0,
    connect_timeout: float = 15.0,
) -> bool:
    stdout, _, code = await ssh_run(
        target.ip,
        _REBOOT_CHECK,
        timeout=timeout,
        username=target.ssh_user,
        port=target.port,
        connect_timeout=connect_timeout,
    )
    if code != 0:
        return False
    return "REBOOT=1" in stdout


def parse_os_release_id(text: str) -> str | None:
    m = re.search(r"^ID=(.+)$", text, re.M)
    if not m:
        return None
    return m.group(1).strip().strip('"')
