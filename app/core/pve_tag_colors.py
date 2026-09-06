"""Proxmox VE tag colors: Datacenter color-map + pve-manager stringToRGB hash.

Matches proxmox-widget-toolkit ``Utils.js`` (``stringToRGB``,
``getTextContrastClass``) and pve-manager ``UIOptions.parseTagOverrides``.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# PVE stringToRGB: blend hashed RGB toward white.
_HASH_ALPHA = 0.7
_HASH_BG = 255.0

# SAPC constants from Proxmox.Utils.getTextContrastClass
_BLK_THRS = 0.022
_BLK_CLMP = 1.414

_HEX6 = re.compile(r"^[0-9a-fA-F]{6}$")
_PROP_PAIR = re.compile(r"(?:^|,)([A-Za-z0-9_-]+)=([^,]*)")


def _to_int32(n: int) -> int:
    n = int(n) & 0xFFFFFFFF
    if n >= 0x80000000:
        return n - 0x100000000
    return n


def parse_color_map(raw: Any) -> dict[str, list[int]]:
    """Parse PVE ``color-map``: ``tag:RRGGBB[:RRGGBB];tag2:…`` → RGB lists."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return extract_color_map(raw)
    text = str(raw).strip()
    if not text:
        return {}
    if "color-map=" in text or ("," in text and "=" in text and ":" in text):
        extracted = _color_map_from_property_string(text)
        if extracted is not None:
            text = extracted
    colors: dict[str, list[int]] = {}
    for part in text.split(";"):
        chunk = part.strip()
        if not chunk:
            continue
        bits = chunk.split(":")
        if len(bits) < 2:
            continue
        tag = bits[0].strip()
        bg_hex = bits[1].strip().removeprefix("#")
        if not tag or not _HEX6.match(bg_hex):
            continue
        rgb = _hex_to_rgb(bg_hex)
        if rgb is None:
            continue
        if len(bits) >= 3:
            fg_hex = bits[2].strip().removeprefix("#")
            fg = _hex_to_rgb(fg_hex) if _HEX6.match(fg_hex) else None
            if fg is not None:
                rgb = rgb + fg
        colors[tag] = rgb
    return colors


def extract_color_map(options: Any) -> dict[str, list[int]]:
    """Pull ``color-map`` from ``GET /cluster/options`` (or a tag-style object)."""
    if options is None:
        return {}
    if isinstance(options, str):
        return parse_color_map(options)
    if not isinstance(options, dict):
        return {}
    style = options.get("tag-style", options)
    if isinstance(style, dict):
        return parse_color_map(style.get("color-map") or "")
    if isinstance(style, str):
        return parse_color_map(style)
    return {}


def _color_map_from_property_string(text: str) -> str | None:
    for match in _PROP_PAIR.finditer(text):
        if match.group(1) == "color-map":
            return match.group(2).strip()
    return None


def _hex_to_rgb(hex6: str) -> list[int] | None:
    try:
        return [
            int(hex6[0:2], 16),
            int(hex6[2:4], 16),
            int(hex6[4:6], 16),
        ]
    except ValueError:
        return None


def string_to_rgb(tag: str) -> list[float]:
    """PVE ``Proxmox.Utils.stringToRGB`` — stable hash, blended toward white."""
    if not tag:
        return [float(_HASH_BG), float(_HASH_BG), float(_HASH_BG)]
    hashed = 0
    for ch in f"{tag}prox":
        shifted = _to_int32(hashed << 5)
        hashed = _to_int32(ord(ch) + (shifted - hashed))
    fade = _HASH_BG * (1.0 - _HASH_ALPHA)
    return [
        (hashed & 255) * _HASH_ALPHA + fade,
        ((hashed >> 8) & 255) * _HASH_ALPHA + fade,
        ((hashed >> 16) & 255) * _HASH_ALPHA + fade,
    ]


def get_text_contrast_class(rgb: list[float] | list[int]) -> str:
    """PVE SAPC helper: ``light`` = white text, ``dark`` = black text."""
    r = (float(rgb[0]) / 255.0) ** 2.4
    g = (float(rgb[1]) / 255.0) ** 2.4
    b = (float(rgb[2]) / 255.0) ** 2.4
    bg = r * 0.2126729 + g * 0.7151522 + b * 0.072175
    if bg <= _BLK_THRS:
        bg = bg + (_BLK_THRS - bg) ** _BLK_CLMP
    contrast_light = bg**0.65 - 1.0
    contrast_dark = bg**0.56 - 0.046134502
    if abs(contrast_light) >= abs(contrast_dark):
        return "light"
    return "dark"


def lookup_color_map(
    tag: str, color_map: Mapping[str, list[int]] | None
) -> list[int] | None:
    """Exact PVE key match (case-sensitive)."""
    if not tag or not color_map:
        return None
    hit = color_map.get(tag)
    if isinstance(hit, list) and len(hit) >= 3:
        return hit
    return None


def resolve_tag_rgb(
    tag: str, color_map: Mapping[str, list[int]] | None = None
) -> list[float]:
    mapped = lookup_color_map(tag, color_map)
    if mapped is not None:
        return [float(c) for c in mapped]
    return string_to_rgb(tag)


def _round_rgb(rgb: list[float] | list[int]) -> tuple[int, int, int]:
    return (
        max(0, min(255, int(round(float(rgb[0]))))),
        max(0, min(255, int(round(float(rgb[1]))))),
        max(0, min(255, int(round(float(rgb[2]))))),
    )


def resolve_tag_appearance(
    tag: str, color_map: Mapping[str, list[int]] | None = None
) -> dict[str, Any]:
    """Background, foreground, contrast class, and whether the map won."""
    mapped = lookup_color_map(tag, color_map)
    rgb = [float(c) for c in mapped] if mapped is not None else string_to_rgb(tag)
    bg = _round_rgb(rgb)
    if mapped is not None and len(mapped) >= 6:
        fg = _round_rgb(mapped[3:6])
        contrast = "dark"
        source = "map"
    else:
        contrast = get_text_contrast_class(rgb[:3])
        fg = (255, 255, 255) if contrast == "light" else (0, 0, 0)
        source = "map" if mapped is not None else "hash"
    return {
        "bg": bg,
        "fg": fg,
        "contrast": contrast,
        "source": source,
        "numeric": bool(tag) and tag.isdigit(),
    }


def tag_chip_vars(
    tag: str, color_map: Mapping[str, list[int]] | None = None
) -> dict[str, Any]:
    """Template helper: inline style + contrast for a chip."""
    appearance = resolve_tag_appearance(tag, color_map)
    bg = appearance["bg"]
    fg = appearance["fg"]
    return {
        "style": (
            f"background-color: rgb({bg[0]}, {bg[1]}, {bg[2]}); "
            f"color: rgb({fg[0]}, {fg[1]}, {fg[2]});"
        ),
        "contrast": appearance["contrast"],
        "source": appearance["source"],
        "numeric": appearance["numeric"],
    }


def serialize_color_map(
    color_map: Mapping[str, list[int]] | None,
) -> dict[str, dict[str, list[int]]]:
    out: dict[str, dict[str, list[int]]] = {}
    for tag, rgb in (color_map or {}).items():
        if not tag or not isinstance(rgb, list) or len(rgb) < 3:
            continue
        row: dict[str, list[int]] = {"bg": [int(rgb[0]), int(rgb[1]), int(rgb[2])]}
        if len(rgb) >= 6:
            row["fg"] = [int(rgb[3]), int(rgb[4]), int(rgb[5])]
        out[str(tag)] = row
    return out
