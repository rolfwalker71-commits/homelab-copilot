"""Unit tests for apply command env, noise filtering, and job liveness."""

from __future__ import annotations

import time
import unittest

from patcher.apply import (
    _apt_cmd,
    is_apply_noise_line,
    is_status_heartbeat_line,
)
from patcher.jobs import JOBS, JobRegistry, PatchJob


STDBUF_ERR = (
    "ERROR: ld.so: object '/usr/libexec/rust-coreutils/libstdbuf.so' "
    "from LD_PRELOAD cannot be preloaded (cannot open shared object file): ignored."
)


class AptCmdTests(unittest.TestCase):
    def test_upgrade_is_fully_noninteractive(self) -> None:
        cmd = _apt_cmd("all", [])
        self.assertIn("DEBIAN_FRONTEND=noninteractive", cmd)
        self.assertIn("DEBIAN_PRIORITY=critical", cmd)
        self.assertIn("DEBCONF_NONINTERACTIVE_SEEN=true", cmd)
        self.assertIn("NEEDRESTART_MODE=a", cmd)
        self.assertIn("force-confold", cmd)
        self.assertIn("force-confdef", cmd)
        self.assertIn("apt-get", cmd)
        self.assertIn("upgrade", cmd)
        self.assertNotIn("stdbuf", cmd)

    def test_selected_and_security_keep_dpkg_opts(self) -> None:
        selected = _apt_cmd("selected", ["curl"])
        self.assertIn("--only-upgrade", selected)
        self.assertIn("force-confold", selected)
        self.assertIn("DEBIAN_PRIORITY=critical", selected)
        sec = _apt_cmd("security", [])
        self.assertIn("unattended-upgrade", sec)
        self.assertIn("force-confold", sec)


class NoiseLineTests(unittest.TestCase):
    def test_rust_coreutils_stdbuf_is_noise(self) -> None:
        self.assertTrue(is_apply_noise_line(STDBUF_ERR))
        self.assertFalse(
            is_apply_noise_line(
                "Setting up keyboard-configuration (1.237ubuntu3.1)..."
            )
        )

    def test_heartbeat_is_status_not_noise(self) -> None:
        line = (
            "SSH-Sitzung offen — keine neue Ausgabe seit 75s "
            "(dpkg configure kann mehrere Minuten still sein)…"
        )
        self.assertTrue(is_status_heartbeat_line(line))
        self.assertFalse(is_apply_noise_line(line))


class JobLivenessTests(unittest.TestCase):
    def test_payload_exposes_alive_and_silence(self) -> None:
        job = PatchJob(id="j1", kind="apply", target_id="h1", status="running")
        job.last_output_at = time.time() - 80
        payload = job.to_dict()
        self.assertTrue(payload["alive"])
        self.assertFalse(payload["done"])
        self.assertGreaterEqual(payload["silence_seconds"], 75)
        self.assertIn("last_output_at", payload)

    def test_stdbuf_error_is_not_logged(self) -> None:
        reg = JobRegistry()
        job = reg.create(kind="apply", target_id="h1")
        before = job.last_output_at
        time.sleep(0.02)
        reg.append_log(job.id, STDBUF_ERR)
        got = reg.get(job.id)
        assert got is not None
        self.assertEqual(got.log_lines, [])
        self.assertEqual(got.last_output_at, before)

    def test_heartbeat_does_not_reset_last_output(self) -> None:
        reg = JobRegistry()
        job = reg.create(kind="apply", target_id="h1")
        reg.append_log(job.id, "Setting up keyboard-configuration (1.237ubuntu3.1)...")
        after_real = reg.get(job.id)
        assert after_real is not None
        stamped = after_real.last_output_at
        time.sleep(0.02)
        reg.append_log(
            job.id,
            "SSH-Sitzung offen — keine neue Ausgabe seit 75s "
            "(dpkg configure kann mehrere Minuten still sein)…",
        )
        got = reg.get(job.id)
        assert got is not None
        self.assertEqual(got.last_output_at, stamped)
        self.assertTrue(any("SSH-Sitzung offen" in line for line in got.log_lines))
        self.assertGreaterEqual(got.updated_at, stamped)

    def test_finished_job_is_not_alive(self) -> None:
        job = PatchJob(id="j2", kind="apply", target_id="h1", status="failed")
        self.assertFalse(job.to_dict()["alive"])
        self.assertTrue(job.to_dict()["done"])


class ModuleJobsSingletonTests(unittest.TestCase):
    def test_global_registry_exists(self) -> None:
        self.assertIsInstance(JOBS, JobRegistry)


if __name__ == "__main__":
    unittest.main()
