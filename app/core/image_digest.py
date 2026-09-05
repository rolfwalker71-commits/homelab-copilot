"""Compare local Docker image digests to a registry manifest (no pull).

Floating tags such as ``:release`` are multi-arch: RepoDigests / image id are
often the platform manifest, while ``imagetools '{{.Manifest.Digest}}'`` is the
index. An update exists only when the two sets do not overlap.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any, Literal

DigestStatus = Literal["current", "update", "unknown"]

_SHA256 = re.compile(r"sha256:[0-9a-fA-F]{64}")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def normalize_digest(value: str) -> str:
    """Return ``sha256:<64 hex>`` or empty if the value is not a full digest."""
    raw = (value or "").strip().lower()
    if "@sha256:" in raw:
        raw = "sha256:" + raw.split("@sha256:", 1)[-1]
    if raw.startswith("sha256:"):
        raw = raw[7:]
    raw = raw.split(",", 1)[0].strip()
    if not _HEX64.match(raw):
        return ""
    return f"sha256:{raw}"


def local_digest_set(*, repo_digests: list[str], image_id: str = "") -> set[str]:
    """Digests that describe the image the container is actually running."""
    out: set[str] = set()
    for item in repo_digests:
        digest = normalize_digest(item)
        if digest:
            out.add(digest)
    digest = normalize_digest(image_id)
    if digest:
        out.add(digest)
    return out


def _digests_from_json(data: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(data, dict):
        for key in ("digest", "Digest"):
            digest = normalize_digest(str(data.get(key) or ""))
            if digest:
                found.add(digest)
        for key in ("Descriptor", "descriptor"):
            desc = data.get(key)
            if isinstance(desc, dict):
                found |= _digests_from_json(desc)
        for key in ("manifest", "Manifest", "SchemaV2Manifest"):
            man = data.get(key)
            if isinstance(man, dict):
                found |= _digests_from_json(man)
        for key in ("manifests", "Manifests"):
            items = data.get(key)
            if isinstance(items, list):
                for item in items:
                    found |= _digests_from_json(item)
    elif isinstance(data, list):
        for item in data:
            found |= _digests_from_json(item)
    return found


def parse_remote_inspect_digests(text: str) -> set[str]:
    """Collect index + platform digests from imagetools / manifest inspect."""
    found: set[str] = set()
    raw = (text or "").strip()
    if not raw:
        return found
    try:
        found |= _digests_from_json(json.loads(raw))
    except json.JSONDecodeError:
        stripped = raw
        start = stripped.find("{")
        if start >= 0:
            try:
                found |= _digests_from_json(json.loads(stripped[start:]))
            except json.JSONDecodeError:
                pass
    for match in _SHA256.finditer(raw):
        digest = normalize_digest(match.group(0))
        if digest:
            found.add(digest)
    return found


def image_digest_status(local: set[str], remote: set[str]) -> DigestStatus:
    """``update`` only when both sides have digests and none match."""
    if not remote or not local:
        return "unknown"
    if local & remote:
        return "current"
    return "update"


def first_digest(values: set[str] | list[str]) -> str:
    for item in values:
        digest = normalize_digest(str(item))
        if digest:
            return digest
    return ""


def cmd_remote_manifest_inspect(image: str) -> str:
    """Manifest-only registry check (no layer pull)."""
    quoted = shlex.quote(image)
    return (
        "docker buildx imagetools inspect --format '{{json .}}' "
        + quoted
        + " 2>/dev/null"
        + " || docker buildx imagetools inspect --format '{{.Digest}}' "
        + quoted
        + " 2>/dev/null"
        + " || docker buildx imagetools inspect "
        + quoted
        + " 2>/dev/null"
        + " || docker manifest inspect --verbose "
        + quoted
        + " 2>/dev/null"
    )


def parse_compose_project_label_lines(text: str) -> dict[str, str]:
    """Parse ``project name`` lines from container inspect."""
    mapping: dict[str, str] = {}
    for line in (text or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        project, name = parts[0].strip(), parts[1].strip().lstrip("/")
        if not project or not name:
            continue
        mapping[name] = project
        mapping[f"/{name}"] = project
    return mapping


def cmd_compose_project_labels(names: list[str]) -> str:
    quoted = " ".join(shlex.quote(n) for n in names if n)
    return (
        "docker inspect --format "
        '\'{{index .Config.Labels "com.docker.compose.project"}} {{.Name}}\' -- '
        + quoted
    )
