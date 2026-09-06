"""Surgical DistUpgrade workarounds for Deb822 + EOL hops.

Ubuntu's plucky (and later) upgrader calls ``e.section['Signed-By']`` on
``ExplodedDeb822SourceEntry`` after ``migrateToDeb822Sources()``. That object
has no ``.section`` (Launchpad #2125393). After we skip that migration so
classic ``.list`` sources stay, ``updateDeb822Sources()`` still assigns
``entry.suites = sorted(...)``. python-apt ``SourceEntry.suites`` is a
read-only property (no setter) — Deb822 entries are writable. AptClone is
optional and often missing on slim LXC images.

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
_UPDATE_DEB822_DEF_RE = re.compile(
    r"^([ \t]*)def updateDeb822Sources\(self[^)]*\):\n",
    re.M,
)
_SUITES_ASSIGN_RE = re.compile(
    r"^([ \t]+)(\S+)\.suites\s*=\s*(.+)$",
    re.M,
)
_ALREADY_GUARDED = re.compile(
    r"hasattr\([^)]+,\s*['\"]section['\"]\)",
)
_DOUPDATE_FAILED_RE = re.compile(
    r'^([ \t]*)logging\.error\((["\'])doUpdate\(\) failed completely\2\)\s*$',
    re.M,
)
_SYSTEM_DIRS_RE = re.compile(
    r"^SYSTEM_DIRS\s*=\s*\[(?:[^\]]|\n)*\]",
    re.M,
)
_KERNEL_ZERO_FALLBACK = (
    "        kernel = 16*1024*1024\n"
)
_INITRD_ZERO_FALLBACK = (
    "        initrd = 175*1024*1024\n"
)


@dataclass(frozen=True)
class ControllerPatchResult:
    text: str
    signed_by_count: int
    migrate_guard: bool
    update_deb822_guard: bool
    suites_assign_count: int
    doupdate_guard: bool
    notes: tuple[str, ...]


@dataclass(frozen=True)
class CachePatchResult:
    text: str
    kernel_zero: bool
    notes: tuple[str, ...]


@dataclass(frozen=True)
class MainPatchResult:
    text: str
    boot_skip: bool
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


def remap_ubuntu_suite(suite: str, from_code: str, to_code: str) -> str:
    """Map ``oracular`` / ``oracular-security`` → ``plucky`` / ``plucky-security``."""
    suite = (suite or "").strip()
    from_code = (from_code or "").strip().lower()
    to_code = (to_code or "").strip().lower()
    if not suite or not from_code or not to_code:
        return suite
    base = suite.lower().split("-", 1)[0]
    if base != from_code:
        return suite
    return to_code + suite[len(base) :]


def patch_migrate_skip_guard(source: str) -> tuple[str, bool]:
    """Inject an early return when the hlops skip-flag file exists."""
    if "migrateToDeb822Sources() skipped by hlops" in source:
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


def patch_update_deb822_skip_guard(source: str) -> tuple[str, bool]:
    """No-op ``updateDeb822Sources`` so classic ``.list`` is not rewritten as Deb822."""
    if "updateDeb822Sources() skipped by hlops" in source:
        return source, False
    m = _UPDATE_DEB822_DEF_RE.search(source)
    if not m:
        return source, False
    indent = m.group(1) + "    "
    guard = (
        f"{indent}import os as _hlops_os\n"
        f"{indent}if _hlops_os.path.exists({HLOPS_SKIP_MIGRATE_FLAG!r}):\n"
        f"{indent}    logging.debug('updateDeb822Sources() skipped by hlops')\n"
        f"{indent}    return True\n"
    )
    return source[: m.end()] + guard + source[m.end() :], True


def _parens_balanced(expr: str) -> bool:
    depth = 0
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def patch_suites_readonly_assign(source: str) -> tuple[str, int]:
    """Do not assign ``entry.suites = …`` on python-apt ``SourceEntry`` (no setter).

    Only wraps a single-line assignment whose RHS has balanced parentheses, so
    multi-line ``sorted([…],`` calls in ``_addDefaultSources`` stay valid.
    """
    if "hlops: SourceEntry.suites has no setter" in source and not _unpatched_suites_assign(
        source
    ):
        return source, 0
    count = 0
    out: list[str] = []
    prev_stripped = ""
    for line in source.splitlines(keepends=True):
        raw = line.splitlines()[0] if line.splitlines() else line
        m = _SUITES_ASSIGN_RE.match(raw)
        if (
            m
            and prev_stripped != "try:"
            and "hlops: SourceEntry.suites has no setter" not in raw
            and _parens_balanced(m.group(3))
        ):
            indent, var, value = m.group(1), m.group(2), m.group(3)
            count += 1
            out.append(
                f"{indent}try:\n"
                f"{indent}    {var}.suites = {value}\n"
                f"{indent}except (AttributeError, TypeError):\n"
                f"{indent}    pass  # hlops: SourceEntry.suites has no setter\n"
            )
            prev_stripped = "pass  # hlops: SourceEntry.suites has no setter"
            continue
        out.append(line)
        stripped = raw.strip()
        if stripped:
            prev_stripped = stripped
    return "".join(out), count


def _unpatched_suites_assign(source: str) -> bool:
    prev = ""
    for raw in source.splitlines():
        m = _SUITES_ASSIGN_RE.match(raw)
        if (
            m
            and prev != "try:"
            and "hlops: SourceEntry.suites has no setter" not in raw
            and _parens_balanced(m.group(3))
        ):
            return True
        stripped = raw.strip()
        if stripped:
            prev = stripped
    return False


def patch_doupdate_continue_on_eol(source: str) -> tuple[str, bool]:
    """After retries, continue when ``cache.update()`` fails on EOL/LXC hops."""
    if "doUpdate() cache.update failed — hlops continues" in source:
        return source, False
    m = _DOUPDATE_FAILED_RE.search(source)
    if not m:
        return source, False
    indent = m.group(1)
    guard = (
        f"{indent}import os as _hlops_os\n"
        f"{indent}if _hlops_os.path.exists({HLOPS_SKIP_MIGRATE_FLAG!r}):\n"
        f"{indent}    logging.warning(\n"
        f"{indent}        'doUpdate() cache.update failed — hlops continues (EOL/LXC)')\n"
        f"{indent}    return True\n"
    )
    return source[: m.end()] + "\n" + guard + source[m.end() :], True


def patch_kernel_initrd_keep_zero(source: str) -> tuple[str, bool]:
    """LXC has no /boot kernel — do not invent 16/175 MiB and abort on space."""
    if "hlops: LXC has no /boot kernel" in source:
        return source, False
    if _KERNEL_ZERO_FALLBACK not in source and _INITRD_ZERO_FALLBACK not in source:
        return source, False
    text = source
    if _KERNEL_ZERO_FALLBACK in text:
        text = text.replace(
            _KERNEL_ZERO_FALLBACK,
            "        kernel = 0  # hlops: LXC has no /boot kernel — keep 0\n",
            1,
        )
    if _INITRD_ZERO_FALLBACK in text:
        text = text.replace(
            _INITRD_ZERO_FALLBACK,
            "        initrd = 0  # hlops: LXC has no /boot initrd — keep 0\n",
            1,
        )
    return text, text != source


def patch_system_dirs_skip_empty_boot(source: str) -> tuple[str, bool]:
    """Drop ``/boot`` from SYSTEM_DIRS when the container has no kernel image."""
    if "hlops: LXC ohne Kernel in /boot" in source:
        return source, False
    m = _SYSTEM_DIRS_RE.search(source)
    if not m:
        return source, False
    inject = (
        "\n# hlops: LXC ohne Kernel in /boot\n"
        "import glob as _hlops_glob\n"
        "if not _hlops_glob.glob('/boot/vmlinuz*') and "
        "not _hlops_glob.glob('/boot/initrd.img*'):\n"
        "    SYSTEM_DIRS = [d for d in SYSTEM_DIRS if d != '/boot']\n"
    )
    return source[: m.end()] + inject + source[m.end() :], True


def apply_extracted_controller_patches(
    source: str, *, skip_migrate: bool
) -> ControllerPatchResult:
    text, signed_n = patch_signed_by_section(source)
    text, suites_n = patch_suites_readonly_assign(text)
    migrate = False
    update_g = False
    doupdate = False
    if skip_migrate:
        text, migrate = patch_migrate_skip_guard(text)
        text, update_g = patch_update_deb822_skip_guard(text)
        text, doupdate = patch_doupdate_continue_on_eol(text)
    notes: list[str] = []
    if signed_n:
        notes.append(
            f"DistUpgradeController.py: _addSecuritySources gegen fehlendes "
            f".section abgesichert ({signed_n}× Signed-By)."
        )
    if suites_n:
        notes.append(
            f"DistUpgradeController.py: entry.suites-Zuweisung abgesichert "
            f"({suites_n}×, SourceEntry.suites ohne Setter)."
        )
    if migrate:
        notes.append(
            "DistUpgradeController.py: migrateToDeb822Sources übersprungen "
            "(LXC/EOL, klassische apt-Quellen)."
        )
    if update_g:
        notes.append(
            "DistUpgradeController.py: updateDeb822Sources übersprungen "
            "(keine Deb822-Rückkonvertierung, klassische .list bleiben)."
        )
    if doupdate:
        notes.append(
            "DistUpgradeController.py: doUpdate() fährt bei cache.update-Fehler "
            "fort (EOL/LXC)."
        )
    if skip_migrate and not migrate and "def migrateToDeb822Sources" not in source:
        notes.append(
            "Hinweis: migrateToDeb822Sources nicht im Tarball — nichts zu überspringen."
        )
    if skip_migrate and not update_g and "def updateDeb822Sources" not in source:
        notes.append(
            "Hinweis: updateDeb822Sources nicht im Tarball — nichts zu überspringen."
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
        update_deb822_guard=update_g,
        suites_assign_count=suites_n,
        doupdate_guard=doupdate,
        notes=tuple(notes),
    )


def apply_extracted_cache_patches(source: str) -> CachePatchResult:
    text, zeroed = patch_kernel_initrd_keep_zero(source)
    notes: list[str] = []
    if zeroed:
        notes.append(
            "DistUpgradeCache.py: Kernel/Initrd-Größe 0 bleibt 0 "
            "(LXC ohne /boot, kein erfundener 16/175-MiB-Puffer)."
        )
    elif "estimate_kernel_initrd_size_in_boot" not in source:
        notes.append(
            "Hinweis: estimate_kernel_initrd_size_in_boot nicht im Tarball."
        )
    return CachePatchResult(text=text, kernel_zero=zeroed, notes=tuple(notes))


def apply_extracted_main_patches(source: str) -> MainPatchResult:
    text, skipped = patch_system_dirs_skip_empty_boot(source)
    notes: list[str] = []
    if skipped:
        notes.append(
            "DistUpgradeMain.py: /boot aus SYSTEM_DIRS genommen, "
            "wenn kein Kernel im Container liegt."
        )
    elif "SYSTEM_DIRS" not in source:
        notes.append("Hinweis: SYSTEM_DIRS nicht im Tarball.")
    return MainPatchResult(text=text, boot_skip=skipped, notes=tuple(notes))


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


def classic_sources_snippet(
    *,
    eol_codenames: tuple[str, ...],
    from_codename: str = "",
    to_codename: str = "",
) -> str:
    """Convert Deb822 ``*.sources`` to ``*.list``; remap suites; old-releases for EOL."""
    codes = " ".join(eol_codenames)
    frm = (from_codename or "").strip().lower()
    to = (to_codename or "").strip().lower()
    return (
        'echo "Normalisiere apt-Quellen auf klassisches sources.list-Format…"\n'
        f'export HLOPS_EOL_CODENAMES="{codes}"\n'
        f'export HLOPS_FROM_SUITE="{frm}"\n'
        f'export HLOPS_TO_SUITE="{to}"\n'
        "python3 - <<'HLOPS_CLASSIC_EOF'\n"
        + _CLASSIC_SOURCES_PY
        + "\nHLOPS_CLASSIC_EOF\n"
        'echo "apt-get update nach Quellen-Umschreibung (old-releases für EOL)…"\n'
        "if ! apt-get update \\\n"
        "  -o DPkg::Lock::Timeout=60 \\\n"
        "  -o Acquire::ForceIPv4=true \\\n"
        "  -o APT::Sandbox::User=root; then\n"
        '  echo "Hinweis: apt-get update unvollständig — DistUpgrade versucht '
        'es erneut (EOL-Spiegel old-releases)."\n'
        "fi\n"
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
FROM = (os.environ.get("HLOPS_FROM_SUITE") or "").strip().lower()
TO = (os.environ.get("HLOPS_TO_SUITE") or "").strip().lower()
OLD = "http://old-releases.ubuntu.com/ubuntu"
ARCHIVE = "http://archive.ubuntu.com/ubuntu"
SECURITY = "http://security.ubuntu.com/ubuntu"
KEYRING = "/usr/share/keyrings/ubuntu-archive-keyring.gpg"
LIST_D = pathlib.Path("/etc/apt/sources.list.d")
MAIN = pathlib.Path("/etc/apt/sources.list")


def suite_base(suite: str) -> str:
    return (suite or "").strip().lower().split("-", 1)[0]


def remap_suite(suite: str) -> str:
    s = (suite or "").strip()
    if not FROM or not TO or not s:
        return s
    base = suite_base(s)
    if base != FROM:
        return s
    return TO + s[len(base):]


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
                suite = remap_suite(suite)
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
    parts = rest.split()
    if not parts:
        return line
    suite = remap_suite(parts[0])
    new_url = mirror_for(suite, m.group(0))
    tail = " ".join([suite, *parts[1:]])
    return line[: m.start()] + new_url + " " + tail


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

if FROM and TO:
    print("Suites umgeschrieben: %s → %s (klassische .list, kein Deb822)." % (FROM, TO))
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
UPDATE_DEB822_DEF_RE = re.compile(
    r"^([ \t]*)def updateDeb822Sources\(self[^)]*\):\n",
    re.M,
)
SUITES_ASSIGN_RE = re.compile(
    r"^([ \t]+)(\S+)\.suites\s*=\s*(.+)$",
    re.M,
)
ALREADY_GUARDED = re.compile(r"hasattr\([^)]+,\s*['\"]section['\"]\)")
DOUPDATE_FAILED_RE = re.compile(
    r'^([ \t]*)logging\.error\((["\'])doUpdate\(\) failed completely\2\)\s*$',
    re.M,
)
SYSTEM_DIRS_RE = re.compile(r"^SYSTEM_DIRS\s*=\s*\[(?:[^\]]|\n)*\]", re.M)
KERNEL_ZERO = "        kernel = 16*1024*1024\n"
INITRD_ZERO = "        initrd = 175*1024*1024\n"


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
    if "migrateToDeb822Sources() skipped by hlops" in source:
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


def patch_update_deb822_skip_guard(source):
    if "updateDeb822Sources() skipped by hlops" in source:
        return source, False
    m = UPDATE_DEB822_DEF_RE.search(source)
    if not m:
        return source, False
    indent = m.group(1) + "    "
    guard = (
        f"{indent}import os as _hlops_os\n"
        f"{indent}if _hlops_os.path.exists({SKIP_FLAG!r}):\n"
        f"{indent}    logging.debug('updateDeb822Sources() skipped by hlops')\n"
        f"{indent}    return True\n"
    )
    return source[: m.end()] + guard + source[m.end():], True


def _parens_balanced(expr):
    depth = 0
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def patch_suites_readonly_assign(source):
    count = 0
    out = []
    prev_stripped = ""
    for line in source.splitlines(keepends=True):
        raw = line.splitlines()[0] if line.splitlines() else line
        m = SUITES_ASSIGN_RE.match(raw)
        if (
            m
            and prev_stripped != "try:"
            and "hlops: SourceEntry.suites has no setter" not in raw
            and _parens_balanced(m.group(3))
        ):
            indent, var, value = m.group(1), m.group(2), m.group(3)
            count += 1
            out.append(
                f"{indent}try:\n"
                f"{indent}    {var}.suites = {value}\n"
                f"{indent}except (AttributeError, TypeError):\n"
                f"{indent}    pass  # hlops: SourceEntry.suites has no setter\n"
            )
            prev_stripped = "pass  # hlops: SourceEntry.suites has no setter"
            continue
        out.append(line)
        stripped = raw.strip()
        if stripped:
            prev_stripped = stripped
    return "".join(out), count


def patch_doupdate_continue_on_eol(source):
    if "doUpdate() cache.update failed — hlops continues" in source:
        return source, False
    m = DOUPDATE_FAILED_RE.search(source)
    if not m:
        return source, False
    indent = m.group(1)
    guard = (
        f"{indent}import os as _hlops_os\n"
        f"{indent}if _hlops_os.path.exists({SKIP_FLAG!r}):\n"
        f"{indent}    logging.warning(\n"
        f"{indent}        'doUpdate() cache.update failed — hlops continues (EOL/LXC)')\n"
        f"{indent}    return True\n"
    )
    return source[: m.end()] + "\n" + guard + source[m.end():], True


def patch_kernel_initrd_keep_zero(source):
    if "hlops: LXC has no /boot kernel" in source:
        return source, False
    text = source
    if KERNEL_ZERO in text:
        text = text.replace(
            KERNEL_ZERO,
            "        kernel = 0  # hlops: LXC has no /boot kernel — keep 0\n",
            1,
        )
    if INITRD_ZERO in text:
        text = text.replace(
            INITRD_ZERO,
            "        initrd = 0  # hlops: LXC has no /boot initrd — keep 0\n",
            1,
        )
    return text, text != source


def patch_system_dirs_skip_empty_boot(source):
    if "hlops: LXC ohne Kernel in /boot" in source:
        return source, False
    m = SYSTEM_DIRS_RE.search(source)
    if not m:
        return source, False
    inject = (
        "\n# hlops: LXC ohne Kernel in /boot\n"
        "import glob as _hlops_glob\n"
        "if not _hlops_glob.glob('/boot/vmlinuz*') and "
        "not _hlops_glob.glob('/boot/initrd.img*'):\n"
        "    SYSTEM_DIRS = [d for d in SYSTEM_DIRS if d != '/boot']\n"
    )
    return source[: m.end()] + inject + source[m.end():], True


extract = os.environ.get("EXTRACT") or ""
if not extract:
    print("Hinweis: EXTRACT fehlt — DistUpgrade-Patch übersprungen.")
    sys.exit(0)
root = pathlib.Path(extract)
ctrl_files = list(root.rglob("DistUpgradeController.py"))
cache_files = list(root.rglob("DistUpgradeCache.py"))
main_files = list(root.rglob("DistUpgradeMain.py"))
if not ctrl_files:
    print("Hinweis: DistUpgradeController.py nicht im Tarball — kein Patch.")
    sys.exit(0)

skip = os.environ.get("HLOPS_SKIP_MIGRATE", "0") == "1"
if skip:
    pathlib.Path(SKIP_FLAG).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(SKIP_FLAG).write_text("1\n", encoding="utf-8")
    print("Quirk: migrateToDeb822Sources und updateDeb822Sources deaktiviert (Flag %s)." % SKIP_FLAG)

for path in ctrl_files:
    text = path.read_text(encoding="utf-8", errors="replace")
    new, n = patch_signed_by_section(text)
    new, suites_n = patch_suites_readonly_assign(new)
    guarded = False
    update_g = False
    doupdate = False
    if skip:
        new, guarded = patch_migrate_skip_guard(new)
        new, update_g = patch_update_deb822_skip_guard(new)
        new, doupdate = patch_doupdate_continue_on_eol(new)
    if new != text:
        path.write_text(new, encoding="utf-8")
    if n:
        print(
            "Gepatcht: %s — _addSecuritySources gegen fehlendes .section "
            "abgesichert (%d× Signed-By)." % (path, n)
        )
    if suites_n:
        print(
            "Gepatcht: %s — entry.suites-Zuweisung abgesichert "
            "(%d×, SourceEntry.suites ohne Setter)." % (path, suites_n)
        )
    if guarded:
        print(
            "Gepatcht: %s — migrateToDeb822Sources übersprungen "
            "(LXC/EOL, klassische apt-Quellen)." % path
        )
    if update_g:
        print(
            "Gepatcht: %s — updateDeb822Sources übersprungen "
            "(keine Deb822-Rückkonvertierung, klassische .list bleiben)." % path
        )
    if doupdate:
        print(
            "Gepatcht: %s — doUpdate() fährt bei cache.update-Fehler "
            "fort (EOL/LXC)." % path
        )
    if skip and not guarded and "def migrateToDeb822Sources" not in text:
        print("Hinweis: migrateToDeb822Sources nicht in %s." % path)
    if skip and not update_g and "def updateDeb822Sources" not in text:
        print("Hinweis: updateDeb822Sources nicht in %s." % path)
    if not n and ".section['Signed-By']" not in text and '.section["Signed-By"]' not in text:
        print("Hinweis: keine Signed-By-Zuweisung in %s (Tarball evtl. schon gefixt)." % path)

for path in cache_files:
    text = path.read_text(encoding="utf-8", errors="replace")
    new, zeroed = patch_kernel_initrd_keep_zero(text)
    if new != text:
        path.write_text(new, encoding="utf-8")
    if zeroed:
        print(
            "Gepatcht: %s — Kernel/Initrd-Größe 0 bleibt 0 "
            "(LXC ohne /boot, kein 16/175-MiB-Puffer)." % path
        )

for path in main_files:
    text = path.read_text(encoding="utf-8", errors="replace")
    new, skipped = patch_system_dirs_skip_empty_boot(text)
    if new != text:
        path.write_text(new, encoding="utf-8")
    if skipped:
        print(
            "Gepatcht: %s — /boot aus SYSTEM_DIRS genommen, "
            "wenn kein Kernel im Container liegt." % path
        )
"""
