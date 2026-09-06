"""Unit tests: Compose labels → docker compose argv / cwd (no SSH)."""

from __future__ import annotations

import unittest

from app.core.compose_apply import (
    compose_file_argv,
    compose_ls_match,
    compose_skip_reason_de,
    compose_spec_from_inspects,
    compose_spec_from_labels,
    compose_stack_argv,
    compose_stack_shell,
    docker_create_argv_from_inspect,
    extra_networks_from_inspect,
    parse_compose_ls_json,
)


class LabelToArgvTests(unittest.TestCase):
    def test_two_relative_files_and_cwd(self) -> None:
        spec = compose_spec_from_labels(
            {
                "com.docker.compose.project": "memos",
                "com.docker.compose.project.working_dir": "/opt/memos",
                "com.docker.compose.project.config_files": "a.yml,b.yml",
            }
        )
        self.assertEqual(spec["project"], "memos")
        self.assertEqual(spec["working_dir"], "/opt/memos")
        self.assertEqual(spec["config_files"], ["a.yml", "b.yml"])
        self.assertEqual(compose_file_argv(spec["config_files"]), ["-f", "a.yml", "-f", "b.yml"])

        argv = compose_stack_argv(
            project=spec["project"],
            config_files=spec["config_files"],
            extra=["pull"],
        )
        self.assertEqual(
            argv,
            ["docker", "compose", "-p", "memos", "-f", "a.yml", "-f", "b.yml", "pull"],
        )
        cmd = compose_stack_shell(working_dir=spec["working_dir"], argv=argv)
        self.assertTrue(cmd.startswith("cd /opt/memos && "))
        self.assertIn("-f a.yml -f b.yml", cmd)
        self.assertIn(" pull", cmd)

    def test_absolute_label_files_stay_absolute(self) -> None:
        spec = compose_spec_from_labels(
            {
                "com.docker.compose.project": "immich",
                "com.docker.compose.project.working_dir": "/opt/immich",
                "com.docker.compose.project.config_files": (
                    "/opt/immich/docker-compose.yml,/opt/immich/docker-compose.override.yml"
                ),
            }
        )
        argv = compose_stack_argv(
            project=spec["project"],
            config_files=spec["config_files"],
            extra=["up", "-d", "--force-recreate"],
        )
        self.assertEqual(
            compose_file_argv(spec["config_files"]),
            [
                "-f",
                "/opt/immich/docker-compose.yml",
                "-f",
                "/opt/immich/docker-compose.override.yml",
            ],
        )
        self.assertIn("--force-recreate", argv)
        self.assertEqual(
            compose_stack_shell(working_dir=spec["working_dir"], argv=argv).split(" && ", 1)[0],
            "cd /opt/immich",
        )

    def test_inspects_merge_labels(self) -> None:
        inspects = [
            {
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": "memos",
                        "com.docker.compose.project.working_dir": "/opt/memos",
                        "com.docker.compose.project.config_files": "docker-compose.yml",
                    }
                }
            }
        ]
        spec = compose_spec_from_inspects(inspects, project="memos")
        self.assertEqual(spec["working_dir"], "/opt/memos")
        self.assertIn("/opt/memos/docker-compose.yml", spec["candidates"])

    def test_compose_ls_match(self) -> None:
        text = (
            '[{"Name":"memos","Status":"running(5)",'
            '"ConfigFiles":"/opt/memos/docker-compose.yml,/opt/memos/override.yml"}]'
        )
        entries = parse_compose_ls_json(text)
        wd, files = compose_ls_match(entries, "memos")
        self.assertEqual(wd, "/opt/memos")
        self.assertEqual(files, ["/opt/memos/docker-compose.yml", "/opt/memos/override.yml"])
        self.assertEqual(compose_ls_match(entries, "other"), (None, []))

    def test_skip_reason_is_german(self) -> None:
        text = compose_skip_reason_de()
        self.assertIn("Keine Compose-Datei", text)
        self.assertIn("docker compose wird übersprungen", text)
        self.assertIn("docker pull", text)

    def test_no_cwd_without_working_dir(self) -> None:
        argv = compose_stack_argv(project="x", config_files=["a.yml"], extra=["pull"])
        self.assertEqual(compose_stack_shell(working_dir=None, argv=argv), " ".join(argv))


class RecreateArgvTests(unittest.TestCase):
    def test_create_preserves_name_image_env_volume(self) -> None:
        info = {
            "Name": "/memos",
            "Config": {
                "Image": "neosmemo/memos:stable",
                "Env": ["TZ=Europe/Berlin"],
                "Labels": {"com.docker.compose.project": "memos"},
                "Hostname": "memos",
            },
            "HostConfig": {
                "RestartPolicy": {"Name": "unless-stopped"},
                "NetworkMode": "memos_default",
                "PortBindings": {"5230/tcp": [{"HostIp": "", "HostPort": "5230"}]},
            },
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": "memos_data",
                    "Destination": "/var/opt/memos",
                    "RW": True,
                }
            ],
            "NetworkSettings": {"Networks": {"memos_default": {}, "extra_net": {}}},
        }
        argv = docker_create_argv_from_inspect(info)
        self.assertEqual(argv[:4], ["docker", "create", "--name", "memos"])
        self.assertIn("--restart", argv)
        self.assertIn("unless-stopped", argv)
        self.assertIn("--network", argv)
        self.assertIn("memos_default", argv)
        self.assertIn("-e", argv)
        self.assertIn("TZ=Europe/Berlin", argv)
        self.assertIn("-v", argv)
        self.assertIn("memos_data:/var/opt/memos", argv)
        self.assertIn("-p", argv)
        self.assertIn("5230:5230/tcp", argv)
        self.assertIn("neosmemo/memos:stable", argv)
        self.assertEqual(extra_networks_from_inspect(info), ["extra_net"])


if __name__ == "__main__":
    unittest.main()
