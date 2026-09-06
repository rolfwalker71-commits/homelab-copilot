"""Surgical DistUpgrade workarounds for Deb822 + EOL hops.

Ubuntu's plucky (and later) upgrader calls ``e.section['Signed-By']`` on
``ExplodedDeb822SourceEntry`` after ``migrateToDeb822Sources()``. That object
has no ``.section`` (Launchpad #2125393). AptClone is optional and often
missing on slim LXC images.

We never vendor DistUpgrade. Each hop patches the extracted tarball in
``/var/tmp/ubuntu-release-upgrader/<codename>.d``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patcher.release import ReleaseHop

HLOPS_SKIP_MIGRATE_FLAG = "/var/tmp/ubuntu-release-upgrader/hlops-skip-migrate-deb822"
UBUNTU_ARCHIVE_KEYRING = "/usr/share/keyrings/ubuntu-archive-keyring.gpg"

_SIGNED_BY_RE = re.compile(
    r"^([ \t]+)(\S+)\.section\[(['\"])Signed-By\3\]\s*=\s*(.+)$",
    re.M,
)
_MIGRATE_DEF_RE = re.compile(
    r"^([ \t]*)def migrateToDeb822Sources\(self[^)]*\):\n",
    re.M,
)
_ALREADY_GUARDED = re.compile(
    r"hasattr\([^)]+,\s*['\"]section['\"]\)",
)


@dataclass(frozen=True)
class ControllerPatchResult:
    text: str
    signed_by_count: int
    migrate_guard: bool
    notes: tuple[str, ...]


def should_skip_migrate_deb822(
    hop: "ReleaseHop",
    *,
    container: bool,
    today: date | None = None,
) -> bool:
    """Skip Deb822 migration on LXC and on hops that touch an EOL series."""
    if container:
        return True
    from patcher.release import ubuntu_series_is_eol

    return ubuntu_series_is_eol(hop.source, today) or ubuntu_series_is_eol(
        hop.target, today
    )


def eol_ubuntu_codenames(today: date | None = None) -> tuple[str, ...]:
    from patcher.release import _UBUNTU_META, ubuntu_series_is_eol

    return tuple(
        meta[0]
        for ver, meta in _UBUNTU_META.items()
        if ubuntu_series_is_eol(ver, today)
    )


def patch_signed_by_section(source: str) -> tuple[str, int]:
    """Guard ``e.section['Signed-By'] = …`` so ExplodedDeb822SourceEntry survives.

    If ``e`` has ``.section`` (classic SourcesList), keep the original write.
    Else set ``e.signed_by``. If neither exists, skip (Signed-By already on disk).
    """
    count = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal count
        indent, var, _quote, value = m.group(1), m.group(2), m.group(3), m.group(4)
        before = source[: m.start()]
        prev_code = [ln for ln in before.splitlines() if ln.strip()]
        if prev_code and _ALREADY_GUARDED.search(prev_code[-1]):
            return m.group(0)
        count += 1
        return (
            f"{indent}if hasattr({var}, 'section'):\n"
            f"{indent}    {var}.section['Signed-By'] = {value}\n"
            f"{indent}elif hasattr({var}, 'signed_by'):\n"
            f"{indent}    {var}.signed_by = {value}\n"
            f"{indent}# hlops: ExplodedDeb822SourceEntry has no .section — skip"
        )

    return _SIGNED_BY_RE.sub(repl, source), count


def patch_migrate_skip_guard(source: str) -> tuple[str, bool]:
    """Inject an early return when the hlops skip-flag file exists."""
    if HLOPS_SKIP_MIGRATE_FLAG in source:
        return source, False
    m = _MIGRATE_DEF_RE.search(source)
    if not m:
        return source, False
    indent = m.group(1) + "    "
    guard = (
        f"{indent}import os as _hlops_os\n"
        f"{indent}if _hlops_os.path.exists({HLOPS_SKIP_MIGRATE_FLAG!r}):\n"
        f"{indent}    logging.debug('migrateToDeb822Sources() skipped by hlops')\n"
        f"{indent}    return\n"
    )
    return source[: m.end()] + guard + source[m.end() :], True


def apply_extracted_controller_patches(
    source: str, *, skip_migrate: bool
) -> ControllerPatchResult:
    text, signed_n = patch_signed_by_section(source)
    migrate = False
    if skip_migrate:
        text, migrate = patch_migrate_skip_guard(text)
    notes: list[str] = []
    if signed_n:
        notes.append(
            f"DistUpgradeController.py: _addSecuritySources gegen fehlendes "
            f".section abgesichert ({signed_n}× Signed-By)."
        )
    if migrate:
        notes.append(
            "DistUpgradeController.py: migrateToDeb822Sources übersprungen "
            "(LXC/EOL, klassische apt-Quellen)."
        )
    if skip_migrate and not migrate and "def migrateToDeb822Sources" not in source:
        notes.append(
            "Hinweis: migrateToDeb822Sources nicht im Tarball — nichts zu überspringen."
        )
    if not signed_n and ".section['Signed-By']" not in source and '.section["Signed-By"]' not in source:
        notes.append(
            "Hinweis: keine e.section['Signed-By']-Zuweisung gefunden — "
            "Tarball evtl. schon gefixt."
        )
    return ControllerPatchResult(
        text=text,
        signed_by_count=signed_n,
        migrate_guard=migrate,
        notes=tuple(notes),
    )


def apt_clone_install_snippet() -> str:
    """Best-effort apt-clone / python3-apt. Missing packages must not fail the hop."""
    return """
echo "Installiere apt-clone und python3-apt (AptClone-Import)…"
if apt-get install -y python3-apt \\
  -o DPkg::Lock::Timeout=60 \\
  -o Acquire::ForceIPv4=true \\
  -o APT::Sandbox::User=root; then
  echo "python3-apt bereit."
else
  echo "Hinweis: python3-apt nicht installierbar — DistUpgrade läuft weiter."
fi
if apt-get install -y apt-clone \\
  -o DPkg::Lock::Timeout=60 \\
  -o Acquire::ForceIPv4=true \\
  -o APT::Sandbox::User=root; then
  echo "apt-clone / python3-apt bereit (AptClone-Import)."
else
  echo "Hinweis: apt-clone nicht in den Quellen — AptClone-Import bleibt ohne Paket."
fi
"""


def classic_sources_snippet(*, eol_codenames: tuple[str, ...]) -> str:
    """Convert Deb822 ``*.sources`` to one-line ``*.list``; point EOL at old-releases."""
    codes = " ".join(eol_codenames)
    return (
        'echo "Normalisiere apt-Quellen auf klassisches sources.list-Format…"\n'
        f'export HLOPS_EOL_CODENAMES="{codes}"\n'
        "python3 - <<'HLOPS_CLASSIC_EOF'\n"
        + _CLASSIC_SOURCES_PY
        + "\nHLOPS_CLASSIC_EOF\n"
    )


def extracted_patcher_snippet(*, skip_migrate: bool) -> str:
    """After extract: patch DistUpgradeController.py and optionally touch the skip flag."""
    flag = "1" if skip_migrate else "0"
    return (
        f"export HLOPS_SKIP_MIGRATE={flag}\n"
        "python3 - <<'HLOPS_PATCH_EOF'\n"
        + _EXTRACTED_PATCHER_PY
        + "\nHLOPS_PATCH_EOF\n"
    )


_CLASSIC_SOURCES_PY = r"""
import os, pathlib, re, shutil

EOL = set((os.environ.get("HLOPS_EOL_CODENAMES") or "").split())
OLD = "http://old-releases.ubuntu.com/ubuntu"
ARCHIVE = "http://archive.ubuntu.com/ubuntu"
SECURITY = "http://security.ubuntu.com/ubuntu"
KEYRING = "/usr/share/keyrings/ubuntu-archive-keyring.gpg"
LIST_D = pathlib.Path("/etc/apt/sources.list.d")
MAIN = pathlib.Path("/etc/apt/sources.list")


def suite_base(suite: str) -> str:
    return (suite or "").strip().lower().split("-", 1)[0]


def mirror_for(suite: str, uri: str) -> str:
    base = suite_base(suite)
    if base in EOL:
        return OLD
    u = (uri or "").rstrip("/")
    if "old-releases.ubuntu.com" in u:
        if "security" in suite:
            return SECURITY
        return ARCHIVE
    return uri.rstrip("/") if uri else ARCHIVE


def parse_deb822(text: str) -> list[dict[str, str]]:
    stanzas, cur, key = [], {}, None
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if line.lstrip().startswith("#"):
            continue
        if not line.strip():
            if cur:
                stanzas.append(cur)
                cur, key = {}, None
            continue
        if line[:1] in " \t" and key:
            cur[key] = (cur.get(key, "") + " " + line.strip()).strip()
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip()
            cur[key] = v.strip()
    if cur:
        stanzas.append(cur)
    return stanzas


def stanza_lines(st: dict[str, str]) -> list[str]:
    enabled = st.get("Enabled", "yes").strip().lower()
    if enabled in ("no", "false", "0"):
        return []
    types = (st.get("Types") or "deb").split()
    uris = (st.get("URIs") or "").split()
    suites = (st.get("Suites") or "").split()
    comps = (st.get("Components") or "").strip()
    signed = (st.get("Signed-By") or KEYRING).strip()
    arches = (st.get("Architectures") or "").strip()
    lines = []
    for typ in types:
        if typ not in ("deb", "deb-src"):
            continue
        for uri in uris:
            for suite in suites:
                mirror = mirror_for(suite, uri)
                opts = [f"signed-by={signed}"]
                if arches:
                    opts.append(f"arch={arches}")
                lines.append(f"{typ} [{','.join(opts)}] {mirror} {suite} {comps}".rstrip())
    return lines


def rewrite_list_line(line: str) -> str:
    if not re.match(r"^\s*deb(-src)?\s", line):
        return line
    m = re.search(r"https?://\S+", line)
    if not m:
        return line
    rest = line[m.end():].strip()
    suite = rest.split()[0] if rest else ""
    new = mirror_for(suite, m.group(0))
    return line[: m.start()] + new + line[m.end():]


changed = 0
list_d = LIST_D
list_d.mkdir(parents=True, exist_ok=True)

for src in sorted(list_d.glob("*.sources")):
    text = src.read_text(encoding="utf-8", errors="replace")
    lines = []
    for st in parse_deb822(text):
        lines.extend(stanza_lines(st))
    dest = src.with_suffix(".list")
    bak = pathlib.Path(str(src) + ".hlops-deb822")
    if dest.exists() and dest != src:
        dest.write_text(
            dest.read_text(encoding="utf-8", errors="replace").rstrip()
            + "\n"
            + "\n".join(lines)
            + "\n",
            encoding="utf-8",
        )
    else:
        dest.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    shutil.move(str(src), str(bak))
    print(f"Deb822 umgewandelt: {src} → {dest}")
    changed += 1

for lst in [MAIN, *sorted(list_d.glob("*.list"))]:
    if not lst.is_file():
        continue
    old = lst.read_text(encoding="utf-8", errors="replace")
    new = "\n".join(rewrite_list_line(ln) for ln in old.splitlines())
    if old.splitlines() != new.splitlines():
        bak = pathlib.Path(str(lst) + ".bak-hlops-classic")
        if not bak.exists():
            shutil.copy2(lst, bak)
        lst.write_text(new + ("\n" if new and not new.endswith("\n") else ""), encoding="utf-8")
        print(f"Spiegel angepasst: {lst}")
        changed += 1

if changed:
    print("Klassische apt-Quellen bereit (old-releases für EOL, archive für aktuelle).")
else:
    print("Keine Deb822-Quellen zum Umwandeln — klassisches Format bleibt.")
"""


_EXTRACTED_PATCHER_PY = r"""
import os, pathlib, re, sys

SKIP_FLAG = "/var/tmp/ubuntu-release-upgrader/hlops-skip-migrate-deb822"
SIGNED_BY_RE = re.compile(
    r"^([ \t]+)(\S+)\.section\[(['\"])Signed-By\3\]\s*=\s*(.+)$",
    re.M,
)
MIGRATE_DEF_RE = re.compile(
    r"^([ \t]*)def migrateToDeb822Sources\(self[^)]*\):\n",
    re.M,
)
ALREADY_GUARDED = re.compile(r"hasattr\([^)]+,\s*['\"]section['\"]\)")


def patch_signed_by_section(source):
    count = 0

    def repl(m):
        nonlocal count
        indent, var, _q, value = m.group(1), m.group(2), m.group(3), m.group(4)
        before = source[: m.start()]
        prev = [ln for ln in before.splitlines() if ln.strip()]
        if prev and ALREADY_GUARDED.search(prev[-1]):
            return m.group(0)
        count += 1
        return (
            f"{indent}if hasattr({var}, 'section'):\n"
            f"{indent}    {var}.section['Signed-By'] = {value}\n"
            f"{indent}elif hasattr({var}, 'signed_by'):\n"
            f"{indent}    {var}.signed_by = {value}\n"
            f"{indent}# hlops: ExplodedDeb822SourceEntry has no .section — skip"
        )

    return SIGNED_BY_RE.sub(repl, source), count


def patch_migrate_skip_guard(source):
    if SKIP_FLAG in source:
        return source, False
    m = MIGRATE_DEF_RE.search(source)
    if not m:
        return source, False
    indent = m.group(1) + "    "
    guard = (
        f"{indent}import os as _hlops_os\n"
        f"{indent}if _hlops_os.path.exists({SKIP_FLAG!r}):\n"
        f"{indent}    logging.debug('migrateToDeb822Sources() skipped by hlops')\n"
        f"{indent}    return\n"
    )
    return source[: m.end()] + guard + source[m.end():], True


extract = os.environ.get("EXTRACT") or ""
if not extract:
    print("Hinweis: EXTRACT fehlt — DistUpgrade-Patch übersprungen.")
    sys.exit(0)
root = pathlib.Path(extract)
files = list(root.rglob("DistUpgradeController.py"))
if not files:
    print("Hinweis: DistUpgradeController.py nicht im Tarball — kein Patch.")
    sys.exit(0)

skip = os.environ.get("HLOPS_SKIP_MIGRATE", "0") == "1"
if skip:
    pathlib.Path(SKIP_FLAG).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(SKIP_FLAG).write_text("1\n", encoding="utf-8")
    print("Quirk: migrateToDeb822Sources deaktiviert (Flag %s)." % SKIP_FLAG)

for path in files:
    text = path.read_text(encoding="utf-8", errors="replace")
    new, n = patch_signed_by_section(text)
    guarded = False
    if skip:
        new, guarded = patch_migrate_skip_guard(new)
    if new != text:
        path.write_text(new, encoding="utf-8")
    if n:
        print(
            "Gepatcht: %s — _addSecuritySources gegen fehlendes .section "
            "abgesichert (%d× Signed-By)." % (path, n)
        )
    if guarded:
        print(
            "Gepatcht: %s — migrateToDeb822Sources übersprungen "
            "(LXC/EOL, klassische apt-Quellen)." % path
        )
    if skip and not guarded and "def migrateToDeb822Sources" not in text:
        print("Hinweis: migrateToDeb822Sources nicht in %s." % path)
    if not n and ".section['Signed-By']" not in text and '.section["Signed-By"]' not in text:
        print("Hinweis: keine Signed-By-Zuweisung in %s (Tarball evtl. schon gefixt)." % path)
"""
