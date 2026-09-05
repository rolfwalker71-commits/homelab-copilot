"""Release-upgrade recommendation (Ubuntu first, Debian suggestion-only).

The *suggested destination* is the next release that is still supported, or the
next LTS — never an already-EOL interim. Ubuntu cannot skip releases:
``do-release-upgrade`` still walks sequential hops after one operator confirm.

As of 2026-09-05: 24.10 / 25.04 / 25.10 are EOL; 26.04 is current LTS.
A 24.10 host is recommended **24.10 → 26.04 LTS** (3 execution hops).
Supported 24.04 LTS only gets a quiet “Nächstes LTS verfügbar”.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from patcher.ubuntu_eol import (
    is_ubuntu_id,
    is_ubuntu_lts,
    parse_ubuntu_version,
    ubuntu_interim_eol_date,
    ubuntu_is_supported_lts,
)

# Known Ubuntu series (version, codename, LTS, first published).
# Do not list unreleased series as upgrade targets.
_UBUNTU_META: dict[str, tuple[str, bool, date]] = {
    "18.04": ("bionic", True, date(2018, 4, 26)),
    "18.10": ("cosmic", False, date(2018, 10, 18)),
    "19.04": ("disco", False, date(2019, 4, 18)),
    "19.10": ("eoan", False, date(2019, 10, 17)),
    "20.04": ("focal", True, date(2020, 4, 23)),
    "20.10": ("groovy", False, date(2020, 10, 22)),
    "21.04": ("hirsute", False, date(2021, 4, 22)),
    "21.10": ("impish", False, date(2021, 10, 14)),
    "22.04": ("jammy", True, date(2022, 4, 21)),
    "22.10": ("kinetic", False, date(2022, 10, 20)),
    "23.04": ("lunar", False, date(2023, 4, 20)),
    "23.10": ("mantic", False, date(2023, 10, 12)),
    "24.04": ("noble", True, date(2024, 4, 25)),
    "24.10": ("oracular", False, date(2024, 10, 10)),
    "25.04": ("plucky", False, date(2025, 4, 17)),
    "25.10": ("questing", False, date(2025, 10, 9)),
    "26.04": ("resolute", True, date(2026, 4, 23)),
}

# Full names for pinned meta-release Dist blocks (fallback: title-cased codename).
_UBUNTU_TITLE: dict[str, str] = {
    "22.04": "Jammy Jellyfish",
    "24.04": "Noble Numbat",
    "24.10": "Oracular Oriole",
    "25.04": "Plucky Puffin",
    "25.10": "Questing Quokka",
    "26.04": "Resolute Raccoon",
}

_ARCHIVE_MIRROR = "http://archive.ubuntu.com/ubuntu"
_OLD_RELEASES_MIRROR = "http://old-releases.ubuntu.com/ubuntu"

# DistUpgradeFetcher / apt log: 'resolute.tar.gz' or 'resolute.tar.gz.gpg'
_UPGRADE_TARBALL_RE = re.compile(r"\b([a-z]{4,})\.tar\.gz(?:\.gpg)?\b")

# Debian: suggestion only. Perform is Ubuntu (do-release-upgrade).
_DEBIAN_NEXT: dict[str, tuple[str, str]] = {
    "bookworm": ("trixie", "13"),
    "12": ("trixie", "13"),
}


def _parse_ym(version: str) -> tuple[int, int] | None:
    parts = (version or "").strip().split(".")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def next_ubuntu_series(version: str) -> str | None:
    """Immediate next Ubuntu series (cannot skip). 24.10 → 25.04."""
    ym = _parse_ym(version)
    if not ym:
        return None
    year, month = ym
    if month == 4:
        return f"{year:02d}.10"
    if month == 10:
        return f"{year + 1:02d}.04"
    return None


def ubuntu_codename(version: str) -> str:
    meta = _UBUNTU_META.get(version)
    return meta[0] if meta else ""


def ubuntu_full_name(version: str) -> str:
    """Animal title for meta-release ``Name:`` (e.g. Plucky Puffin)."""
    titled = _UBUNTU_TITLE.get(version)
    if titled:
        return titled
    code = ubuntu_codename(version)
    return code.title() if code else version


def ubuntu_version_for_codename(codename: str) -> str:
    code = (codename or "").strip().lower()
    if not code:
        return ""
    for ver, meta in _UBUNTU_META.items():
        if meta[0] == code:
            return ver
    return ""


def ubuntu_pretty(version: str, *, lts: bool | None = None) -> str:
    is_lts = is_ubuntu_lts(version) if lts is None else lts
    suffix = " LTS" if is_lts else ""
    name = ubuntu_codename(version)
    extra = f" ({name})" if name else ""
    return f"Ubuntu {version}{suffix}{extra}"


def ubuntu_is_released(version: str, today: date | None = None) -> bool:
    today = today or date.today()
    meta = _UBUNTU_META.get(version)
    if not meta:
        return False
    return meta[2] <= today


def ubuntu_series_is_supported(version: str, today: date | None = None) -> bool:
    """True if the series is still a reasonable upgrade *destination*."""
    today = today or date.today()
    if not version or not ubuntu_is_released(version, today):
        return False
    if is_ubuntu_lts(version):
        return ubuntu_is_supported_lts(version)
    eol = ubuntu_interim_eol_date(version)
    if eol is None:
        return False
    return today < eol


def ubuntu_series_is_eol(version: str, today: date | None = None) -> bool:
    today = today or date.today()
    if not version:
        return False
    if is_ubuntu_lts(version):
        return not ubuntu_is_supported_lts(version)
    eol = ubuntu_interim_eol_date(version)
    return bool(eol and today >= eol)


def ubuntu_is_current_devel(version: str, today: date | None = None) -> bool:
    """True only if the series is listed but not yet released (needs ``-d``)."""
    today = today or date.today()
    if not version or version not in _UBUNTU_META:
        return False
    return not ubuntu_is_released(version, today)


def should_use_devel_flag(hop: "ReleaseHop", today: date | None = None) -> bool:
    """``do-release-upgrade -d`` only when this hop's target is current devel."""
    return ubuntu_is_current_devel(hop.target, today)


def ubuntu_upgrade_tool_mirror(version: str, today: date | None = None) -> str:
    """old-releases for EOL series (25.04/25.10 as of 2026-09), else archive."""
    if ubuntu_series_is_eol(version, today):
        return _OLD_RELEASES_MIRROR
    return _ARCHIVE_MIRROR


def _dist_upgrader_tarball_url(host: str, code: str, pocket: str | None = None) -> str:
    """``dists/<codename>/…/<codename>.tar.gz`` — never ``*-updates`` / ``*-security``."""
    dist = pocket or code
    return f"{host}/dists/{dist}/main/dist-upgrader-all/current/{code}.tar.gz"


# HEAD 2026-09-05: 25.04/25.10 tarballs still on archive; old-releases 404s.
# 26.04 is on archive under dists/resolute/ (also under -updates; we never use that).
_DIST_UPGRADER_FIRST_HOST: dict[str, str] = {
    "plucky": _ARCHIVE_MIRROR,
    "questing": _ARCHIVE_MIRROR,
    "resolute": _ARCHIVE_MIRROR,
}


def upgrade_tool_url_candidates(version: str, today: date | None = None) -> list[str]:
    """DistUpgrade tarball URLs under ``dists/<codename>/`` (never ``*-updates``).

    First success wins. As of 2026-09-05, 25.04/25.10 are EOL but Ubuntu left
    ``plucky.tar.gz`` / ``questing.tar.gz`` on archive, not old-releases.
    ``*-proposed`` is a last-resort pocket only.
    """
    code = ubuntu_codename(version)
    if not code:
        return []
    first = _DIST_UPGRADER_FIRST_HOST.get(code)
    if first is None:
        first = (
            _OLD_RELEASES_MIRROR
            if ubuntu_series_is_eol(version, today)
            else _ARCHIVE_MIRROR
        )
    other = (
        _ARCHIVE_MIRROR if first == _OLD_RELEASES_MIRROR else _OLD_RELEASES_MIRROR
    )
    urls = [
        _dist_upgrader_tarball_url(first, code),
        _dist_upgrader_tarball_url(other, code),
    ]
    if ubuntu_series_is_eol(version, today):
        urls.append(
            _dist_upgrader_tarball_url(_OLD_RELEASES_MIRROR, code, f"{code}-proposed")
        )
        urls.append(
            _dist_upgrader_tarball_url(_ARCHIVE_MIRROR, code, f"{code}-proposed")
        )
    return urls


def _meta_release_block(
    version: str, *, supported: bool, today: date | None = None
) -> str:
    code = ubuntu_codename(version)
    title = ubuntu_full_name(version)
    meta = _UBUNTU_META.get(version)
    pub = meta[2] if meta else date(2000, 1, 1)
    date_s = pub.strftime("%a, %d %B %Y 00:00:00 UTC")
    mirror = ubuntu_upgrade_tool_mirror(version, today)
    candidates = upgrade_tool_url_candidates(version, today)
    tool = candidates[0] if candidates else _dist_upgrader_tarball_url(mirror, code)
    lts = " LTS" if is_ubuntu_lts(version) else ""
    notes = (
        f"{mirror}/dists/{code}/main/dist-upgrader-all/current/ReleaseAnnouncement"
        if supported and not ubuntu_series_is_eol(version, today)
        else "http://changelogs.ubuntu.com/EOLReleaseAnnouncement"
    )
    return (
        f"Dist: {code}\n"
        f"Name: {title}\n"
        f"Version: {version}{lts}\n"
        f"Date: {date_s}\n"
        f"Supported: {1 if supported else 0}\n"
        f"Description: This is the {version}{lts} release\n"
        f"Release-File: {mirror}/dists/{code}/Release\n"
        f"ReleaseNotes: {notes}\n"
        f"UpgradeTool: {tool}\n"
        f"UpgradeToolSignature: {tool}.gpg\n"
    )


def build_meta_release_pin(
    *, source: str, target: str, today: date | None = None
) -> str:
    """Local meta-release: current + hop target only. Target is Supported: 1.

    Official changelogs skip Supported: 0 interims (plucky/questing) and jump
    to the next Supported: 1 (resolute). Pinning prevents that skip.
    """
    today = today or date.today()
    src_supported = not ubuntu_series_is_eol(source, today)
    return (
        _meta_release_block(source, supported=src_supported, today=today)
        + "\n"
        + _meta_release_block(target, supported=True, today=today)
    )


def detect_fetched_upgrade_codename(text: str) -> str:
    """Last known Ubuntu codename mentioned as ``*.tar.gz`` in upgrader output."""
    found = ""
    for match in _UPGRADE_TARBALL_RE.finditer(text or ""):
        name = match.group(1)
        if ubuntu_version_for_codename(name):
            found = name
    return found


def upgrader_log_tail(blob: str, n: int = 30) -> str:
    """Last ``n`` non-empty lines of DistUpgrade/SSH output (not the prefix).

    DistUpgradeViewNonInteractive writes errors to main.log; the hop script
    appends that tail. The operator must see *those* lines, not apt-get's
    first 600 characters (which stop at „Tarball gefunden“).
    """
    lines = [ln.rstrip() for ln in (blob or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    return "\n".join(lines[-max(1, n) :])


def hop_failure_message(hop: "ReleaseHop", code: int, blob: str) -> str:
    """German error: intended hop, leaked tarball if any, Proxmox snap rollback."""
    leaked = detect_fetched_upgrade_codename(blob)
    want = (hop.target_codename or ubuntu_codename(hop.target) or "").lower()
    head = f"do-release-upgrade fehlgeschlagen ({hop.label}, exit {code}):"
    extra = ""
    if leaked and leaked != want:
        ver = ubuntu_version_for_codename(leaked)
        extra = (
            f" Falsches Upgrade-Ziel: {leaked}.tar.gz"
            + (f" (Ubuntu {ver})" if ver else "")
            + f" statt {want}.tar.gz ({hop.target})."
        )
    snap = (
        " Ein Proxmox-Snapshot (hlops-*) vor dem Versuch kann zur Rückkehr "
        "genutzt werden."
    )
    tail = upgrader_log_tail(blob, 30)
    if tail:
        return f"{head}{extra}{snap}\n{tail}"
    return f"{head}{extra}{snap}"


def next_ubuntu_lts(version: str, today: date | None = None) -> str | None:
    """Next *released* LTS after ``version`` (22.04 → 24.04, not 26.04)."""
    today = today or date.today()
    cur = version
    seen: set[str] = set()
    while cur and cur not in seen:
        seen.add(cur)
        nxt = next_ubuntu_series(cur)
        if not nxt:
            return None
        if is_ubuntu_lts(nxt) and ubuntu_is_released(nxt, today):
            return nxt
        cur = nxt
        if len(seen) > 12:
            return None
    return None


def sequential_hops(current: str, target: str) -> list[tuple[str, str]]:
    """Inclusive path of immediate hops. 24.10 → 26.04 ⇒ three pairs."""
    if not current or not target or current == target:
        return []
    hops: list[tuple[str, str]] = []
    cur = current
    guard = 0
    while cur != target and guard < 16:
        nxt = next_ubuntu_series(cur)
        if not nxt:
            return []
        hops.append((cur, nxt))
        cur = nxt
        guard += 1
    if cur != target:
        return []
    return hops


@dataclass
class ReleaseHop:
    source: str
    target: str
    source_codename: str = ""
    target_codename: str = ""
    target_is_lts: bool = False
    prompt: str = "normal"  # lts | normal
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "source_codename": self.source_codename,
            "target_codename": self.target_codename,
            "target_is_lts": self.target_is_lts,
            "prompt": self.prompt,
            "label": self.label,
        }


@dataclass
class ReleaseSuggestion:
    available: bool
    family: str  # ubuntu | debian
    urgency: str  # recommended | optional
    current_version: str
    current_codename: str
    current_pretty: str
    current_is_lts: bool
    current_eol: bool
    target_version: str
    target_codename: str
    target_pretty: str
    target_is_lts: bool
    chip: str
    headline: str
    reason: str
    path_label: str
    method: str  # do-release-upgrade | debian-suggest
    hops: list[ReleaseHop] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    performable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "family": self.family,
            "urgency": self.urgency,
            "current_version": self.current_version,
            "current_codename": self.current_codename,
            "current_pretty": self.current_pretty,
            "current_is_lts": self.current_is_lts,
            "current_eol": self.current_eol,
            "target_version": self.target_version,
            "target_codename": self.target_codename,
            "target_pretty": self.target_pretty,
            "target_is_lts": self.target_is_lts,
            "chip": self.chip,
            "headline": self.headline,
            "reason": self.reason,
            "path_label": self.path_label,
            "method": self.method,
            "performable": self.performable,
            "hop_count": len(self.hops),
            "hops": [h.to_dict() for h in self.hops],
            "alternatives": list(self.alternatives),
        }


def _ubuntu_hops(current: str, target: str) -> list[ReleaseHop]:
    """LTS→LTS is one Prompt=lts hop; otherwise sequential Prompt=normal hops."""
    if not current or not target or current == target:
        return []
    if is_ubuntu_lts(current) and is_ubuntu_lts(target):
        return [
            ReleaseHop(
                source=current,
                target=target,
                source_codename=ubuntu_codename(current),
                target_codename=ubuntu_codename(target),
                target_is_lts=True,
                prompt="lts",
                label=f"{current} → {target} LTS",
            )
        ]
    pairs = sequential_hops(current, target)
    out: list[ReleaseHop] = []
    total = len(pairs)
    for i, (src, dst) in enumerate(pairs, start=1):
        dst_lts = is_ubuntu_lts(dst)
        out.append(
            ReleaseHop(
                source=src,
                target=dst,
                source_codename=ubuntu_codename(src),
                target_codename=ubuntu_codename(dst),
                target_is_lts=dst_lts,
                prompt="lts" if dst_lts and is_ubuntu_lts(src) else "normal",
                label=f"Schritt {i}/{total}: {src} → {dst}"
                + (" LTS" if dst_lts else ""),
            )
        )
    return out


def _path_label(current: str, target: str, hops: list[ReleaseHop]) -> str:
    if not hops:
        return ""
    if len(hops) == 1:
        extra = " LTS" if hops[0].target_is_lts else ""
        return f"{current} → {target}{extra}"
    chain = [hops[0].source] + [h.target for h in hops]
    return (
        f"Ziel {target}"
        + (" LTS" if is_ubuntu_lts(target) else "")
        + f" · {len(hops)} Schritte ({' → '.join(chain)})"
    )


def suggest_ubuntu_release(
    *,
    version_id: str = "",
    pretty_name: str = "",
    codename: str = "",
    today: date | None = None,
) -> ReleaseSuggestion | None:
    today = today or date.today()
    ver = parse_ubuntu_version(version_id, pretty_name)
    if not ver:
        return None
    current_lts = is_ubuntu_lts(ver)
    current_eol = ubuntu_series_is_eol(ver, today)
    current_supported = ubuntu_series_is_supported(ver, today)
    code = (codename or "").strip() or ubuntu_codename(ver)
    pretty = pretty_name.strip() or ubuntu_pretty(ver)

    next_lts = next_ubuntu_lts(ver, today)
    nxt = next_ubuntu_series(ver)
    next_supported = bool(nxt and ubuntu_series_is_supported(nxt, today))

    target: str | None = None
    urgency = "recommended"
    alternatives: list[dict[str, Any]] = []

    if current_lts and current_supported:
        if not next_lts:
            return None
        target = next_lts
        urgency = "optional"
    elif current_eol:
        if next_lts:
            target = next_lts
        elif next_supported:
            target = nxt
        else:
            return None
        urgency = "recommended"
    else:
        # Supported interim: next supported release if LTS is not out yet;
        # if next LTS is already released, prefer LTS (default).
        if next_lts:
            target = next_lts
            urgency = "optional"
            if next_supported and nxt != next_lts:
                alternatives.append(
                    {
                        "target_version": nxt,
                        "target_pretty": ubuntu_pretty(nxt),
                        "note": "Nächstes Release (kein LTS)",
                    }
                )
        elif next_supported:
            target = nxt
            urgency = "optional"
        else:
            return None

    if not target or target == ver:
        return None
    if ubuntu_series_is_eol(target, today) or not ubuntu_is_released(target, today):
        return None

    hops = _ubuntu_hops(ver, target)
    if not hops:
        return None

    target_lts = is_ubuntu_lts(target)
    target_pretty = ubuntu_pretty(target)
    if urgency == "recommended":
        chip = "Release-Upgrade empfohlen"
        headline = f"Release-Upgrade empfohlen: {ver} → {target}" + (
            " LTS" if target_lts else ""
        )
        if current_eol and not current_lts:
            eol = ubuntu_interim_eol_date(ver)
            since = f" (seit {eol.isoformat()})" if eol else ""
            reason = (
                f"{pretty} ist am Ende / kein LTS{since}. "
                f"Nächstes unterstütztes Ziel: {target_pretty}."
            )
        elif current_eol:
            reason = (
                f"{pretty} ist End-of-Life. "
                f"Nächstes unterstütztes LTS: {target_pretty}."
            )
        else:
            reason = f"Upgrade auf {target_pretty} empfohlen."
    else:
        chip = "Nächstes LTS verfügbar" if target_lts else "Release verfügbar"
        headline = (
            f"Nächstes LTS {target} verfügbar"
            if target_lts
            else f"Nächstes Release: {ver} → {target}"
        )
        reason = (
            f"{pretty} wird weiter unterstützt. "
            f"Optional: Upgrade auf {target_pretty}."
        )

    return ReleaseSuggestion(
        available=True,
        family="ubuntu",
        urgency=urgency,
        current_version=ver,
        current_codename=code,
        current_pretty=pretty,
        current_is_lts=current_lts,
        current_eol=current_eol,
        target_version=target,
        target_codename=ubuntu_codename(target),
        target_pretty=target_pretty,
        target_is_lts=target_lts,
        chip=chip,
        headline=headline,
        reason=reason,
        path_label=_path_label(ver, target, hops),
        method="do-release-upgrade",
        hops=hops,
        alternatives=alternatives,
        performable=True,
    )


def suggest_debian_release(
    *,
    version_id: str = "",
    pretty_name: str = "",
    codename: str = "",
) -> ReleaseSuggestion | None:
    """Clear bookworm→trixie suggestion only. Not performed automatically."""
    code = (codename or "").strip().lower()
    ver = (version_id or "").strip()
    key = code or ver
    nxt = _DEBIAN_NEXT.get(key)
    if not nxt:
        return None
    target_code, target_ver = nxt
    pretty = pretty_name.strip() or f"Debian {code or ver}"
    current_label = code or ver or "Debian"
    return ReleaseSuggestion(
        available=True,
        family="debian",
        urgency="optional",
        current_version=ver or code,
        current_codename=code,
        current_pretty=pretty,
        current_is_lts=True,
        current_eol=False,
        target_version=target_ver,
        target_codename=target_code,
        target_pretty=f"Debian {target_code} ({target_ver})",
        target_is_lts=True,
        chip="Nächstes Release verfügbar",
        headline=f"Nächstes Release verfügbar: {current_label} → {target_code}",
        reason=(
            f"{pretty} kann nach Debian {target_code} wechseln. "
            "Automatisches Einspielen ist nur für Ubuntu (do-release-upgrade) "
            "umgesetzt — Debian bitte manuell planen."
        ),
        path_label=f"{current_label} → {target_code}",
        method="debian-suggest",
        hops=[
            ReleaseHop(
                source=current_label,
                target=target_code,
                source_codename=code,
                target_codename=target_code,
                target_is_lts=True,
                prompt="",
                label=f"{current_label} → {target_code}",
            )
        ],
        performable=False,
    )


def suggest_release_upgrade(
    *,
    distro: str | None,
    version_id: str = "",
    pretty_name: str = "",
    codename: str = "",
    today: date | None = None,
) -> ReleaseSuggestion | None:
    d = (distro or "").strip().lower()
    if is_ubuntu_id(d):
        return suggest_ubuntu_release(
            version_id=version_id,
            pretty_name=pretty_name,
            codename=codename,
            today=today,
        )
    if d == "debian":
        return suggest_debian_release(
            version_id=version_id,
            pretty_name=pretty_name,
            codename=codename,
        )
    return None


def suggestion_to_summary(suggestion: ReleaseSuggestion | None) -> dict[str, Any] | None:
    if suggestion is None or not suggestion.available:
        return None
    return suggestion.to_dict()


def parse_virt_fields(fields: dict[str, str]) -> tuple[str, bool]:
    virt = (fields.get("VIRT") or "unknown").strip().lower() or "unknown"
    if virt in ("none", "n/a"):
        virt = "none"
    container = (fields.get("CONTAINER") or "").strip() == "1"
    if virt in ("lxc", "lxc-libvirt", "docker", "podman", "container", "openvz"):
        container = True
    return virt, container
