"""Unit tests for image digest comparison (no live Docker / SSH)."""

from __future__ import annotations

import unittest

from app.core.image_digest import (
    cmd_remote_manifest_inspect,
    first_digest,
    image_digest_status,
    local_digest_set,
    normalize_digest,
    parse_compose_project_label_lines,
    parse_remote_inspect_digests,
)

INDEX = "aa" * 32
PLATFORM = "bb" * 32
OTHER = "cc" * 32
OLD = "dd" * 32


class NormalizeTests(unittest.TestCase):
    def test_repo_digest_and_bare(self) -> None:
        self.assertEqual(
            normalize_digest(f"ghcr.io/immich-app/immich-server@sha256:{INDEX}"),
            f"sha256:{INDEX}",
        )
        self.assertEqual(normalize_digest(f"sha256:{INDEX}"), f"sha256:{INDEX}")
        self.assertEqual(normalize_digest(INDEX), f"sha256:{INDEX}")

    def test_rejects_tags(self) -> None:
        self.assertEqual(normalize_digest("ghcr.io/immich-app/immich-server:release"), "")
        self.assertEqual(normalize_digest("sha256:not-a-digest"), "")


class CompareTests(unittest.TestCase):
    def test_release_tag_index_vs_platform_is_current(self) -> None:
        """Classic false positive: local platform / image id, remote index."""
        local = local_digest_set(
            repo_digests=[f"ghcr.io/immich-app/immich-server@sha256:{PLATFORM}"],
            image_id=f"sha256:{PLATFORM}",
        )
        remote = parse_remote_inspect_digests(
            f'{{"digest":"sha256:{INDEX}","manifests":['
            f'{{"digest":"sha256:{PLATFORM}","platform":{{"architecture":"amd64"}}}}'
            f"]}}"
        )
        self.assertEqual(image_digest_status(local, remote), "current")

    def test_local_image_id_matches_remote_platform(self) -> None:
        local = local_digest_set(repo_digests=[], image_id=f"sha256:{PLATFORM}")
        remote = {f"sha256:{INDEX}", f"sha256:{PLATFORM}"}
        self.assertEqual(image_digest_status(local, remote), "current")

    def test_real_update_when_no_overlap(self) -> None:
        local = local_digest_set(
            repo_digests=[f"ghcr.io/foo@sha256:{OLD}"],
            image_id=f"sha256:{OLD}",
        )
        remote = {f"sha256:{INDEX}", f"sha256:{PLATFORM}"}
        self.assertEqual(image_digest_status(local, remote), "update")

    def test_unknown_without_remote_is_not_an_update(self) -> None:
        local = local_digest_set(repo_digests=[f"sha256:{PLATFORM}"])
        self.assertEqual(image_digest_status(local, set()), "unknown")

    def test_unknown_without_local_is_not_an_update(self) -> None:
        self.assertEqual(image_digest_status(set(), {f"sha256:{INDEX}"}), "unknown")

    def test_old_manifest_digest_only_compare_was_wrong(self) -> None:
        """Previously we compared one local RepoDigest to .Manifest.Digest only."""
        local_one = normalize_digest(
            f"ghcr.io/immich-app/immich-server@sha256:{PLATFORM}"
        )
        remote_index_only = normalize_digest(f"sha256:{INDEX}")
        self.assertNotEqual(local_one, remote_index_only)
        local = {local_one}
        remote = {remote_index_only, f"sha256:{PLATFORM}"}
        self.assertEqual(image_digest_status(local, remote), "current")


class ParseRemoteTests(unittest.TestCase):
    def test_imagetools_json(self) -> None:
        text = (
            '{"name":"ghcr.io/immich-app/immich-server:release",'
            f'"digest":"sha256:{INDEX}",'
            f'"manifests":[{{"digest":"sha256:{PLATFORM}"}}]}}'
        )
        self.assertEqual(
            parse_remote_inspect_digests(text),
            {f"sha256:{INDEX}", f"sha256:{PLATFORM}"},
        )

    def test_human_digest_line(self) -> None:
        text = (
            "Name:      ghcr.io/immich-app/immich-server:release\n"
            f"Digest:    sha256:{INDEX}\n"
            f"  Digest:    sha256:{PLATFORM}\n"
        )
        self.assertIn(f"sha256:{INDEX}", parse_remote_inspect_digests(text))
        self.assertIn(f"sha256:{PLATFORM}", parse_remote_inspect_digests(text))

    def test_verbose_descriptor(self) -> None:
        text = (
            '{"Ref":"ghcr.io/immich-app/immich-server:release",'
            f'"Descriptor":{{"digest":"sha256:{INDEX}"}}}}'
        )
        self.assertEqual(parse_remote_inspect_digests(text), {f"sha256:{INDEX}"})

    def test_empty(self) -> None:
        self.assertEqual(parse_remote_inspect_digests(""), set())
        self.assertEqual(parse_remote_inspect_digests("<no value>"), set())


class HelperTests(unittest.TestCase):
    def test_inspect_command_does_not_pull(self) -> None:
        cmd = cmd_remote_manifest_inspect("ghcr.io/immich-app/immich-server:release")
        self.assertNotIn("docker pull", cmd)
        self.assertNotIn("compose pull", cmd)
        self.assertIn("imagetools inspect", cmd)
        self.assertIn("manifest inspect", cmd)

    def test_first_digest(self) -> None:
        self.assertEqual(first_digest({f"sha256:{OTHER}"}), f"sha256:{OTHER}")
        self.assertEqual(first_digest(set()), "")

    def test_compose_label_lines(self) -> None:
        text = "immich /immich_server\nimmich /immich_machine_learning\n /orphan\n"
        mapping = parse_compose_project_label_lines(text)
        self.assertEqual(mapping["immich_server"], "immich")
        self.assertEqual(mapping["immich_machine_learning"], "immich")
        self.assertNotIn("orphan", mapping)


if __name__ == "__main__":
    unittest.main()
