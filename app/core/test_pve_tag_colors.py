"""PVE tag color-map parsing + stable stringToRGB hash."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from app.config import Settings
from app.core.discovery import DiscoveryEngine
from app.core.proxmox import ProxmoxEndpoint
from app.core.pve_tag_colors import (
    extract_color_map,
    get_text_contrast_class,
    parse_color_map,
    resolve_tag_appearance,
    serialize_color_map,
    string_to_rgb,
    tag_chip_vars,
)


class ColorMapParseTests(unittest.TestCase):
    def test_parses_semicolon_map(self) -> None:
        parsed = parse_color_map("docker:5FA8D3;foo:FF8800")
        self.assertEqual(parsed["docker"], [0x5F, 0xA8, 0xD3])
        self.assertEqual(parsed["foo"], [0xFF, 0x88, 0x00])

    def test_parses_optional_font_hex(self) -> None:
        parsed = parse_color_map("example:000000:FFFFFF")
        self.assertEqual(parsed["example"], [0, 0, 0, 255, 255, 255])

    def test_extracts_from_cluster_options(self) -> None:
        parsed = extract_color_map(
            {"tag-style": {"color-map": "docker:5FA8D3;foo:FF8800", "shape": "circle"}}
        )
        self.assertEqual(parsed["docker"], [95, 168, 211])
        self.assertEqual(parsed["foo"], [255, 136, 0])

    def test_extracts_from_property_string(self) -> None:
        parsed = extract_color_map(
            "color-map=docker:5FA8D3;foo:FF8800,shape=circle"
        )
        self.assertEqual(parsed["docker"], [95, 168, 211])

    def test_skips_invalid_entries(self) -> None:
        parsed = parse_color_map("ok:AABBCC;bad:xyz;:112233;nohex")
        self.assertEqual(list(parsed), ["ok"])
        self.assertEqual(parsed["ok"], [0xAA, 0xBB, 0xCC])

    def test_serialize_roundtrip_shape(self) -> None:
        raw = parse_color_map("docker:5FA8D3;x:010203:0A0B0C")
        dumped = serialize_color_map(raw)
        self.assertEqual(dumped["docker"]["bg"], [95, 168, 211])
        self.assertNotIn("fg", dumped["docker"])
        self.assertEqual(dumped["x"]["fg"], [10, 11, 12])


class TagHashTests(unittest.TestCase):
    def test_docker_hash_is_stable(self) -> None:
        rgb = string_to_rgb("docker")
        again = string_to_rgb("docker")
        self.assertEqual(rgb, again)
        self.assertEqual(
            [round(c) for c in rgb],
            [round(c) for c in string_to_rgb("docker")],
        )
        # Locked against pve-manager Utils.stringToRGB("docker")
        self.assertEqual([round(c) for c in rgb], [174, 77, 166])

    def test_paperlessngx_hash_is_stable(self) -> None:
        rgb = string_to_rgb("paperlessngx")
        self.assertEqual(rgb, string_to_rgb("paperlessngx"))
        self.assertEqual([round(c) for c in rgb], [80, 91, 191])

    def test_distinct_tags_differ(self) -> None:
        docker = [round(c) for c in string_to_rgb("docker")]
        paper = [round(c) for c in string_to_rgb("paperlessngx")]
        portainer = [round(c) for c in string_to_rgb("portainer")]
        self.assertNotEqual(docker, paper)
        self.assertNotEqual(docker, portainer)
        self.assertNotEqual(paper, portainer)

    def test_color_map_wins_over_hash(self) -> None:
        mapped = parse_color_map("docker:5FA8D3")
        appearance = resolve_tag_appearance("docker", mapped)
        self.assertEqual(appearance["bg"], (95, 168, 211))
        self.assertEqual(appearance["source"], "map")
        hashed = resolve_tag_appearance("docker", {})
        self.assertEqual(hashed["source"], "hash")
        self.assertNotEqual(hashed["bg"], appearance["bg"])

    def test_map_lookup_is_case_sensitive(self) -> None:
        mapped = parse_color_map("docker:5FA8D3")
        self.assertEqual(resolve_tag_appearance("Docker", mapped)["source"], "hash")

    def test_contrast_picks_light_or_dark(self) -> None:
        self.assertEqual(get_text_contrast_class([20, 20, 20]), "light")
        self.assertEqual(get_text_contrast_class([240, 240, 220]), "dark")

    def test_chip_vars_include_inline_style(self) -> None:
        chip = tag_chip_vars("docker")
        self.assertIn("background-color: rgb(174, 77, 166)", chip["style"])
        self.assertIn(chip["contrast"], {"light", "dark"})


class TagStyleCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_color_map_cached_per_endpoint(self) -> None:
        engine = DiscoveryEngine(
            Settings(proxmox_host="pve01", proxmox_token_secret="s", proxmox_password="")
        )
        ep = ProxmoxEndpoint(
            id="primary", host="192.168.5.101", token_id="id", token_secret="s"
        )
        client = AsyncMock()
        engine._proxmox_authed_client = AsyncMock(return_value=(client, {}))
        engine._proxmox_get = AsyncMock(
            return_value={"tag-style": {"color-map": "docker:5FA8D3"}}
        )
        first = await engine.fetch_tag_color_map(endpoint=ep)
        second = await engine.fetch_tag_color_map(endpoint=ep)
        self.assertEqual(engine._proxmox_get.await_count, 1)
        self.assertEqual(first["docker"], [95, 168, 211])
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
