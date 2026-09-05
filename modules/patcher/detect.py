"""Detect distro and package manager over SSH."""

from __future__ import annotations

import re
from dataclasses import dataclass

from patcher.release import parse_virt_fields
from patcher.sshutil import ssh_run
from patcher.targets import PatchTarget


@dataclass
class HostDetect:
    pm: str  # apt | dnf | yum | apk
    distro: str
    pretty_name: str
    version_id: str = ""
    version_codename: str = ""
    virt: str = "unknown"
    container: bool = False
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
  echo "VERSION_CODENAME=${VERSION_CODENAME:-}"
else
  echo "ID="
  echo "PRETTY_NAME="
  echo "VERSION_ID="
  echo "VERSION_CODENAME="
fi
VIRT=unknown
CONTAINER=0
if [ -f /.dockerenv ]; then
  VIRT=docker; CONTAINER=1
elif [ -f /run/.containerenv ]; then
  VIRT=podman; CONTAINER=1
elif command -v systemd-detect-virt >/dev/null 2>&1; then
  V=$(systemd-detect-virt 2>/dev/null || true)
  if [ -n "$V" ] && [ "$V" != none ]; then VIRT=$V; fi
  if systemd-detect-virt -q --container 2>/dev/null; then CONTAINER=1; fi
fi
if [ "$CONTAINER" != 1 ]; then
  if [ -d /dev/.lxc ] || grep -qa container=lxc /proc/1/environ 2>/dev/null; then
    VIRT=lxc; CONTAINER=1
  elif [ -f /run/systemd/container ]; then
    VIRT=$(head -c 64 /run/systemd/container 2>/dev/null || echo container)
    CONTAINER=1
  fi
fi
echo "VIRT=$VIRT"
echo "CONTAINER=$CONTAINER"
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
    version_id = fields.get("VERSION_ID") or ""
    codename = fields.get("VERSION_CODENAME") or ""
    virt, container = parse_virt_fields(fields)
    return HostDetect(
        pm=pm,
        distro=distro,
        pretty_name=pretty,
        version_id=version_id,
        version_codename=codename,
        virt=virt,
        container=container,
        raw_os_release=stdout,
    )


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
