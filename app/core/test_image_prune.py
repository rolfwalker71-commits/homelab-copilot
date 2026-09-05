"""Unit tests for unused-image prune helpers (no live Docker / SSH)."""

from __future__ import annotations

import unittest

from app.core.image_prune import (
    cmd_dangling_image_prune,
    cmd_in_use_image_ids,
    cmd_named_image_ids,
    cmd_project_image_ids,
    cmd_rmi_unused,
    compose_projects_for_container_names,
    docker_rmi_ref,
    format_unused_image_cleanup_message,
    normalize_image_id,
    parse_image_id_lines,
    parse_image_prune_output,
    replaced_image_ids_to_remove,
)


class NormalizeImageIdTests(unittest.TestCase):
    def test_sha256_prefix_and_bare(self) -> None:
        hid = "a" * 64
        self.assertEqual(normalize_image_id(f"sha256:{hid}"), hid)
        self.assertEqual(normalize_image_id(hid), hid)
        self.assertEqual(normalize_image_id(f"  SHA256:{hid.upper()}  "), hid)

    def test_rejects_tags_and_garbage(self) -> None:
        self.assertEqual(normalize_image_id("nginx:1.27"), "")
        self.assertEqual(normalize_image_id("ghcr.io/foo/bar:latest"), "")
        self.assertEqual(normalize_image_id("sha256:not-hex"), "")
        self.assertEqual(normalize_image_id(""), "")
        self.assertEqual(normalize_image_id("$(reboot)"), "")


class ParseAndDecisionTests(unittest.TestCase):
    def test_parse_image_id_lines(self) -> None:
        old = "b" * 64
        new = "c" * 64
        text = f"sha256:{old}\n{new}\nnginx:latest\n"
        self.assertEqual(parse_image_id_lines(text), {old, new})

    def test_removes_replaced_unused_only(self) -> None:
        old = "1" * 64
        new = "2" * 64
        other = "3" * 64
        still_used_old = "4" * 64
        safe = replaced_image_ids_to_remove(
            previous_ids={old, still_used_old, f"sha256:{old}"},
            current_ids={f"sha256:{new}"},
            in_use_ids={new, still_used_old, other},
        )
        self.assertEqual(safe, [old])

    def test_keeps_image_still_used_by_other_stack(self) -> None:
        shared = "ab" * 32
        current = "cd" * 32
        safe = replaced_image_ids_to_remove(
            previous_ids={shared},
            current_ids={current},
            in_use_ids={current, shared},
        )
        self.assertEqual(safe, [])

    def test_keeps_unchanged_current_image(self) -> None:
        same = "ef" * 32
        safe = replaced_image_ids_to_remove(
            previous_ids={same},
            current_ids={same},
            in_use_ids={same},
        )
        self.assertEqual(safe, [])


class PruneOutputTests(unittest.TestCase):
    def test_parse_prune_with_space(self) -> None:
        text = (
            "Deleted Images:\n"
            "untagged: ghcr.io/foo/bar@sha256:aaa\n"
            "deleted: sha256:bbbb\n"
            "deleted: sha256:cccc\n"
            "Total reclaimed space: 1.234GB\n"
        )
        parsed = parse_image_prune_output(text)
        self.assertEqual(parsed["deleted"], 2)
        self.assertEqual(parsed["untagged"], 1)
        self.assertEqual(parsed["reclaimed"], "1.234GB")

    def test_parse_nothing_reclaimed(self) -> None:
        parsed = parse_image_prune_output("Total reclaimed space: 0B")
        self.assertEqual(parsed["deleted"], 0)
        self.assertEqual(parsed["reclaimed"], "0B")


class MessageAndCommandTests(unittest.TestCase):
    def test_warning_does_not_fail_upgrade(self) -> None:
        msg = format_unused_image_cleanup_message(warning="exit 1: permission denied")
        self.assertIn("Warnung", msg)
        self.assertIn("erfolgreich", msg)
        self.assertIn("optional", msg)

    def test_deleted_and_reclaimed(self) -> None:
        msg = format_unused_image_cleanup_message(
            dangling_deleted=1,
            replaced_removed=2,
            reclaimed="240MB",
        )
        self.assertEqual(msg, "Ungenutzte Images entfernt: 3 Image(s), 240MB freigegeben.")

    def test_none_to_remove(self) -> None:
        self.assertEqual(
            format_unused_image_cleanup_message(),
            "Keine ungenutzten Images zum Entfernen.",
        )

    def test_prune_command_is_not_nuclear(self) -> None:
        cmd = cmd_dangling_image_prune()
        self.assertEqual(cmd, "docker image prune -f")
        self.assertNotIn("system prune", cmd)
        self.assertNotIn("--all", cmd)
        self.assertNotIn("--volumes", cmd)

    def test_rmi_rejects_force_and_tags(self) -> None:
        hid = "a1b2c3d4e5f6" + "0" * 52
        cmd = cmd_rmi_unused([hid, "nginx:latest", "sha256:" + hid])
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertTrue(cmd.startswith("docker rmi -- "))
        self.assertNotIn(" -f", cmd)
        self.assertIn(f"sha256:{hid}", cmd)
        self.assertNotIn("nginx", cmd)

    def test_project_and_named_commands_quote(self) -> None:
        self.assertIn("com.docker.compose.project=immich", cmd_project_image_ids("immich"))
        self.assertIn("immich_db", cmd_named_image_ids(["immich_db"]))
        self.assertIn("docker ps -aq", cmd_in_use_image_ids())

    def test_compose_project_grouping(self) -> None:
        mapping = {"web": "shop", "db": "shop", "mail": "mailcow"}
        projects, leftover = compose_projects_for_container_names(
            mapping, ["web", "mail", "orphan"]
        )
        self.assertEqual(projects, ["shop", "mailcow"])
        self.assertEqual(leftover, ["orphan"])

    def test_single_project_no_leftover(self) -> None:
        projects, leftover = compose_projects_for_container_names(
            {"a": "stack", "b": "stack"}, ["a", "b"]
        )
        self.assertEqual(projects, ["stack"])
        self.assertEqual(leftover, [])

    def test_rmi_ref_none_for_tag(self) -> None:
        self.assertIsNone(docker_rmi_ref("postgres:16"))


if __name__ == "__main__":
    unittest.main()
