"""Detect Ubuntu end-of-life releases and rewrite apt sources to old-releases.

Never rewrite Debian or Ubuntu LTS that still use archive/security mirrors.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

# Interim (non-LTS) Ubuntu — official / typical EOL. Today ≥ date → archived.
_INTERIM_EOL: dict[str, date] = {
    "18.10": date(2019, 7, 18),
    "19.04": date(2020, 1, 23),
    "19.10": date(2020, 7, 17),
    "20.10": date(2021, 7, 22),
    "21.04": date(2022, 1, 20),
    "21.10": date(2022, 7, 14),
    "22.10": date(2023, 7, 20),
    "23.04": date(2024, 1, 25),
    "23.10": date(2024, 7, 11),
    "24.10": date(2025, 7, 10),
    "25.04": date(2026, 1, 17),
    "25.10": date(2026, 7, 9),
}

# LTS that have fully left archive.ubuntu.com (not ESM-on-archive).
_ARCHIVED_LTS = frozenset({"12.04", "14.04", "16.04"})

# Still served from archive.ubuntu.com / security.ubuntu.com — never rewrite
# just because apt-get update failed (clock / Spiegel / IPv6).
_SUPPORTED_LTS = frozenset({"18.04", "20.04", "22.04", "24.04", "26.04"})

_VER_RE = re.compile(r"\b(\d{2}\.\d{2})\b")
_EOL_APT = re.compile(
    r"release file.*(expired|no longer|not valid)|"
    r"does not have a release file|"
    r"no longer has a release file|"
    r"the repository .+ no longer|"
    r"404\s+not found|"
    r"failed to fetch\s+https?://\S*ubuntu\.com|"
    r"e: the repository",
    re.I,
)


def is_ubuntu_id(distro: str | None) -> bool:
    d = (distro or "").strip().lower()
    return d in ("ubuntu", "ubuntu-core") or d.startswith("ubuntu")


def parse_ubuntu_version(*texts: str | None) -> str:
    for raw in texts:
        if not raw:
            continue
        m = _VER_RE.search(str(raw))
        if m:
            return m.group(1)
    return ""


def is_ubuntu_lts(version_id: str) -> bool:
    """Even-year .04 is LTS (16.04, 18.04, 20.04, 22.04, 24.04, 26.04)."""
    m = re.fullmatch(r"(\d{2})\.04", (version_id or "").strip())
    if not m:
        return False
    return int(m.group(1)) % 2 == 0


def _interim_eol_date(version_id: str) -> date | None:
    known = _INTERIM_EOL.get(version_id)
    if known:
        return known
    m = re.fullmatch(r"(\d{2})\.(\d{2})", version_id)
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if month == 4 and year % 2 == 1:
        # Odd-year April interim ≈ 9 months → mid-January next year
        return date(2000 + year + 1, 1, 17)
    if month == 10:
        return date(2000 + year + 1, 7, 11)
    return None


def ubuntu_eol_reason(
    *,
    distro: str | None,
    version_id: str = "",
    pretty_name: str = "",
    today: date | None = None,
) -> str | None:
    """German reason if this Ubuntu release belongs on old-releases; else None."""
    if not is_ubuntu_id(distro):
        return None
    ver = parse_ubuntu_version(version_id, pretty_name)
    if not ver:
        return None
    today = today or date.today()
    label = pretty_name.strip() or f"Ubuntu {ver}"

    if ver in _ARCHIVED_LTS:
        return (
            f"{label} ist End-of-Life — Paketquellen werden auf "
            "old-releases.ubuntu.com umgestellt."
        )
    if ver in _SUPPORTED_LTS or is_ubuntu_lts(ver):
        return None

    eol = _interim_eol_date(ver)
    if eol and today >= eol:
        return (
            f"{label} ist End-of-Life (seit {eol.isoformat()}) — "
            "Paketquellen werden auf old-releases.ubuntu.com umgestellt."
        )
    return None


def should_rewrite_after_apt_error(
    *,
    distro: str | None,
    version_id: str = "",
    pretty_name: str = "",
    apt_output: str = "",
) -> bool:
    """Rewrite only when apt looks like an expired Ubuntu archive, never current LTS."""
    if not is_ubuntu_id(distro):
        return False
    if not looks_like_eol_apt_error(apt_output):
        return False
    ver = parse_ubuntu_version(version_id, pretty_name)
    if ver in _SUPPORTED_LTS or (ver and is_ubuntu_lts(ver) and ver not in _ARCHIVED_LTS):
        return False
    return True


def looks_like_eol_apt_error(text: str) -> bool:
    return bool(_EOL_APT.search(text or ""))


def rewrite_ubuntu_sources_cmd() -> str:
    """Remote sh: archive/security/ports → old-releases. Echo German status lines."""
    return r"""
export LC_ALL=C
CHANGED=0
rewrite_file() {
  f="$1"
  [ -f "$f" ] || return 0
  if ! grep -qE 'archive\.ubuntu\.com|security\.ubuntu\.com|ports\.ubuntu\.com' "$f"; then
    return 0
  fi
  cp -a -- "$f" "$f.bak-eol" 2>/dev/null || cp -a -- "$f" "$f.bak-eol"
  sed -E \
    -e 's#https?://[A-Za-z0-9.-]*archive\.ubuntu\.com#http://old-releases.ubuntu.com#g' \
    -e 's#https?://[A-Za-z0-9.-]*security\.ubuntu\.com#http://old-releases.ubuntu.com#g' \
    -e 's#https?://[A-Za-z0-9.-]*ports\.ubuntu\.com#http://old-releases.ubuntu.com#g' \
    "$f" > "$f.eoltmp" && mv -f -- "$f.eoltmp" "$f"
  echo "Umgestellt: $f"
  CHANGED=1
}
for f in /etc/apt/sources.list; do rewrite_file "$f"; done
for f in /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
  rewrite_file "$f"
done
if [ "$CHANGED" = 1 ]; then
  echo EOL_REWRITE=1
else
  echo EOL_REWRITE=0
  echo "Keine Ubuntu-Spiegel zum Umstellen gefunden (bereits old-releases oder andere Quellen)."
fi
"""


def detect_fields(detect: Any) -> tuple[str, str, str]:
    distro = getattr(detect, "distro", "") or ""
    version_id = getattr(detect, "version_id", "") or ""
    pretty = getattr(detect, "pretty_name", "") or ""
    return str(distro), str(version_id), str(pretty)
