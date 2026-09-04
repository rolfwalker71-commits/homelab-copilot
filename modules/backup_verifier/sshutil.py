"""SSH helpers for backup: long-running commands + SCP."""

from __future__ import annotations

import asyncio
import base64
import logging
import shlex
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import asyncssh

from app.config import Settings
from app.core.docker_control import DockerControlError, ssh_key_path

logger = logging.getLogger(__name__)

LogFn = Callable[[str], Awaitable[None]]

# Keepalive so idle long sessions are not dropped by NAT/firewall mid-command.
_SSH_KEEPALIVE_INTERVAL = 30
_SSH_KEEPALIVE_COUNT_MAX = 3


def _ssh_connect_kwargs(
    settings: Settings,
    *,
    username: str | None = None,
    key: Path | None = None,
    key_pem: str | None = None,
    password: str | None = None,
    port: int | None = None,
) -> dict:
    connect_timeout = min(10.0, max(3.0, settings.docker_ssh_timeout + 2.0))
    kwargs: dict = {
        "port": port or settings.docker_ssh_port,
        "username": username or settings.docker_ssh_user,
        "known_hosts": None,
        "connect_timeout": connect_timeout,
        "login_timeout": connect_timeout,
        "keepalive_interval": _SSH_KEEPALIVE_INTERVAL,
        "keepalive_count_max": _SSH_KEEPALIVE_COUNT_MAX,
    }
    if password:
        kwargs["password"] = password
    if key_pem:
        try:
            kwargs["client_keys"] = [asyncssh.import_private_key(key_pem)]
        except (ValueError, asyncssh.KeyImportError) as exc:
            raise DockerControlError(
                f"SSH-Key (PEM) ungültig: {exc}",
                status_code=400,
            ) from exc
    elif key is not None:
        kwargs["client_keys"] = [str(key)]
    elif not password:
        # Key auth only when no password — never silent Docker-key fallback
        # for intentional password mode (caller must pass password).
        kwargs["client_keys"] = [str(ssh_key_path(settings))]
    if password and "client_keys" not in kwargs:
        # Prefer password; avoid probing keys that yield misleading Permission denied.
        kwargs["preferred_auth"] = ("password", "keyboard-interactive")
    return kwargs


def format_ssh_failure(ip: str, exc: BaseException, *, username: str | None = None) -> str:
    """Map asyncssh/OS errors to clear German messages (auth vs shell vs other)."""
    text = str(exc).strip()
    low = text.lower()
    user = (username or "").strip()
    who = f" für Benutzer {user}" if user else ""

    if isinstance(exc, asyncssh.PermissionDenied) or "permission denied" in low:
        return (
            f"Authentifizierung fehlgeschlagen{who} auf Host {ip}. "
            f"Passwort/Key prüfen (nicht Port-22-SFTP-Test mit interaktivem ssh)."
        )
    if (
        "shell request failed" in low
        or "pty allocation" in low
        or "channel open failure" in low
        or ("channel" in low and "failed" in low)
    ):
        return (
            f"SSH-Shell auf {ip} nicht verfügbar{who}. "
            f"Hetzner Storage Box: Port 23 + „SSH-Unterstützung“ in der Console; "
            f"Port 22 ist nur SFTP ohne Shell."
        )
    return f"SSH zu {ip} fehlgeschlagen: {text}"


async def ssh_run(
    settings: Settings,
    ip: str,
    cmd: str,
    *,
    timeout: float = 120.0,
    username: str | None = None,
    key: Path | None = None,
    key_pem: str | None = None,
    password: str | None = None,
    port: int | None = None,
) -> tuple[str, str, int]:
    try:
        async with asyncssh.connect(
            ip,
            **_ssh_connect_kwargs(
                settings,
                username=username,
                key=key,
                key_pem=key_pem,
                password=password,
                port=port,
            ),
        ) as conn:
            result = await conn.run(cmd, check=False, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise DockerControlError(
            f"SSH-Timeout zu {ip} — Befehl zu lang oder Host nicht erreichbar?",
            status_code=504,
        ) from exc
    except (OSError, asyncssh.Error) as exc:
        logger.warning("Backup SSH %s: %s", ip, exc)
        raise DockerControlError(
            format_ssh_failure(ip, exc, username=username),
            status_code=502,
        ) from exc

    stdout = result.stdout if isinstance(result.stdout, str) else (result.stdout or b"").decode(
        "utf-8", errors="replace"
    )
    stderr = result.stderr if isinstance(result.stderr, str) else (result.stderr or b"").decode(
        "utf-8", errors="replace"
    )
    return stdout, stderr, int(result.exit_status or 0)


async def ssh_run_ok(
    settings: Settings,
    ip: str,
    cmd: str,
    *,
    timeout: float = 120.0,
    username: str | None = None,
    key: Path | None = None,
    key_pem: str | None = None,
    password: str | None = None,
    port: int | None = None,
) -> str:
    stdout, stderr, code = await ssh_run(
        settings,
        ip,
        cmd,
        timeout=timeout,
        username=username,
        key=key,
        key_pem=key_pem,
        password=password,
        port=port,
    )
    if code != 0:
        detail = (stderr or stdout or "").strip() or f"exit {code}"
        raise DockerControlError(
            f"Remote-Befehl fehlgeschlagen ({ip}): {detail}",
            status_code=502,
        )
    return stdout


async def scp_get(
    settings: Settings,
    ip: str,
    remote_path: str,
    local_path: Path,
    *,
    timeout: float = 600.0,
    username: str | None = None,
    key: Path | None = None,
    key_pem: str | None = None,
    password: str | None = None,
    port: int | None = None,
) -> None:
    """Copy remote → local via SFTP."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with asyncssh.connect(
            ip,
            **_ssh_connect_kwargs(
                settings,
                username=username,
                key=key,
                key_pem=key_pem,
                password=password,
                port=port,
            ),
        ) as conn:
            async with conn.start_sftp_client() as sftp:
                await asyncio.wait_for(
                    sftp.get(remote_path, str(local_path)),
                    timeout=timeout,
                )
    except asyncio.TimeoutError as exc:
        raise DockerControlError(
            f"SCP-Timeout von {ip}:{remote_path}",
            status_code=504,
        ) from exc
    except (OSError, asyncssh.Error) as exc:
        raise DockerControlError(
            format_ssh_failure(ip, exc, username=username),
            status_code=502,
        ) from exc


async def scp_put(
    settings: Settings,
    ip: str,
    local_path: Path,
    remote_path: str,
    *,
    timeout: float = 600.0,
    username: str | None = None,
    key: Path | None = None,
    key_pem: str | None = None,
    password: str | None = None,
    port: int | None = None,
) -> None:
    """Copy local → remote via SFTP."""
    try:
        async with asyncssh.connect(
            ip,
            **_ssh_connect_kwargs(
                settings,
                username=username,
                key=key,
                key_pem=key_pem,
                password=password,
                port=port,
            ),
        ) as conn:
            # Ensure remote parent exists (Storage Box shell: no &&; mkdir -p ok on Synology)
            parent = str(Path(remote_path).parent)
            if parent not in (".", "", "/"):
                # Prefer -p; fall back to plain mkdir (Hetzner whitelist)
                r = await conn.run(
                    f"mkdir -p {shlex.quote(parent)}", check=False, timeout=30
                )
                if r.exit_status not in (0, None) and parent.startswith("/home"):
                    await conn.run(f"mkdir {shlex.quote(parent)}", check=False, timeout=30)
            async with conn.start_sftp_client() as sftp:
                await asyncio.wait_for(
                    sftp.put(str(local_path), remote_path),
                    timeout=timeout,
                )
    except asyncio.TimeoutError as exc:
        raise DockerControlError(
            f"SCP-Timeout nach {ip}:{remote_path}",
            status_code=504,
        ) from exc
    except (OSError, asyncssh.Error) as exc:
        raise DockerControlError(
            format_ssh_failure(ip, exc, username=username),
            status_code=502,
        ) from exc


def _job_paths(work_dir: str, job_name: str) -> dict[str, str]:
    base = f"{work_dir.rstrip('/')}/.hc_job_{job_name}"
    return {
        "script": f"{base}.sh",
        "status": f"{base}.status",
        "log": f"{base}.log",
        "pid": f"{base}.pid",
        "progress": f"{base}.progress",
    }


async def run_detached_and_poll(
    settings: Settings,
    ip: str | None,
    script: str,
    *,
    work_dir: str,
    job_name: str,
    local: bool,
    overall_timeout: float = 3600.0,
    poll_interval: float = 5.0,
    short_timeout: float = 30.0,
    log: LogFn | None = None,
) -> None:
    """Write script on target, start with nohup, poll status via short SSH calls.

    Status file values: ``running``, ``ok``, or ``failed:<exitcode>``.
    Avoids holding one SSH session open for multi-minute tar jobs.
    """
    paths = _job_paths(work_dir, job_name)
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    # Launcher: decode script, mark running, nohup wrapper updates status on EXIT.
    # Paths via env so bash -c '...' stays robust (no nested quoting).
    start_cmd = f"""
set -euo pipefail
mkdir -p -- {shlex.quote(work_dir)}
printf '%s' {shlex.quote(b64)} | base64 -d > {shlex.quote(paths["script"])}
printf 'running\\n' > {shlex.quote(paths["status"])}
printf 'starting\\n' > {shlex.quote(paths["progress"])}
: > {shlex.quote(paths["log"])}
export HC_JOB_STATUS={shlex.quote(paths["status"])}
export HC_JOB_LOG={shlex.quote(paths["log"])}
export HC_JOB_SCRIPT={shlex.quote(paths["script"])}
nohup bash -c '
  set +e
  bash "$HC_JOB_SCRIPT" >>"$HC_JOB_LOG" 2>&1
  ec=$?
  if [ "$ec" -eq 0 ]; then
    printf "ok\\n" > "$HC_JOB_STATUS"
  else
    printf "failed:%s\\n" "$ec" > "$HC_JOB_STATUS"
  fi
' >/dev/null 2>&1 &
echo $! > {shlex.quote(paths["pid"])}
"""
    if local:
        await local_run_ok(start_cmd, timeout=short_timeout)
    else:
        assert ip is not None
        await ssh_run_ok(settings, ip, start_cmd, timeout=short_timeout)

    deadline = time.monotonic() + overall_timeout
    last_progress = ""
    poll_cmd = (
        f"cat {shlex.quote(paths['status'])} 2>/dev/null; echo '---'; "
        f"cat {shlex.quote(paths['progress'])} 2>/dev/null; echo '---'; "
        f"if [ -f {shlex.quote(paths['pid'])} ]; then "
        f"  pid=$(cat {shlex.quote(paths['pid'])}); "
        f"  if kill -0 \"$pid\" 2>/dev/null; then echo alive; else echo dead; fi; "
        f"else echo nopid; fi"
    )

    while True:
        if time.monotonic() > deadline:
            kill_cmd = (
                f"if [ -f {shlex.quote(paths['pid'])} ]; then "
                f"  kill $(cat {shlex.quote(paths['pid'])}) 2>/dev/null || true; "
                f"fi; "
                f"printf 'failed:timeout\\n' > {shlex.quote(paths['status'])}"
            )
            try:
                if local:
                    await local_run(kill_cmd, timeout=short_timeout)
                else:
                    assert ip is not None
                    await ssh_run(settings, ip, kill_cmd, timeout=short_timeout)
            except DockerControlError:
                pass
            raise DockerControlError(
                f"Archiv-Job Timeout nach {int(overall_timeout)}s "
                f"(Volumes/Binds zu groß oder Host langsam)",
                status_code=504,
            )

        if local:
            out, _, _ = await local_run(poll_cmd, timeout=short_timeout)
        else:
            assert ip is not None
            out, _, _ = await ssh_run(settings, ip, poll_cmd, timeout=short_timeout)

        parts = out.split("---")
        status_line = (parts[0] if parts else "").strip().splitlines()
        status = status_line[-1].strip() if status_line else ""
        progress = ""
        if len(parts) > 1:
            prog_lines = parts[1].strip().splitlines()
            progress = prog_lines[-1].strip() if prog_lines else ""
        alive = ""
        if len(parts) > 2:
            alive = parts[2].strip().splitlines()[-1].strip() if parts[2].strip() else ""

        if progress and progress != last_progress and log is not None:
            last_progress = progress
            await log(f"Archiv: {progress}")

        if status == "ok":
            return
        if status.startswith("failed:"):
            tail_cmd = f"tail -n 40 -- {shlex.quote(paths['log'])} 2>/dev/null || true"
            if local:
                log_tail, _, _ = await local_run(tail_cmd, timeout=short_timeout)
            else:
                assert ip is not None
                log_tail, _, _ = await ssh_run(settings, ip, tail_cmd, timeout=short_timeout)
            detail = (log_tail or "").strip() or status
            raise DockerControlError(
                f"Remote-Archiv-Job fehlgeschlagen ({status}): {detail[-500:]}",
                status_code=502,
            )

        # If process died without writing status, treat as failure after grace
        if status == "running" and alive == "dead":
            await asyncio.sleep(1.0)
            if local:
                out2, _, _ = await local_run(
                    f"cat {shlex.quote(paths['status'])} 2>/dev/null || true",
                    timeout=short_timeout,
                )
            else:
                assert ip is not None
                out2, _, _ = await ssh_run(
                    settings,
                    ip,
                    f"cat {shlex.quote(paths['status'])} 2>/dev/null || true",
                    timeout=short_timeout,
                )
            status2 = out2.strip().splitlines()[-1].strip() if out2.strip() else ""
            if status2 == "ok":
                return
            if status2.startswith("failed:"):
                raise DockerControlError(
                    f"Remote-Archiv-Job fehlgeschlagen ({status2})",
                    status_code=502,
                )
            raise DockerControlError(
                "Remote-Archiv-Job beendet ohne Status (Prozess tot)",
                status_code=502,
            )

        await asyncio.sleep(poll_interval)


async def remote_sha256(
    settings: Settings,
    ip: str,
    remote_path: str,
    *,
    timeout: float = 120.0,
    username: str | None = None,
    key: Path | None = None,
    key_pem: str | None = None,
    password: str | None = None,
    port: int | None = None,
) -> str:
    # Storage Box (port 23): no pipes/||/awk — plain sha256sum only.
    # Synology/full shell: fall back to shasum if needed.
    quoted = shlex.quote(remote_path)
    run_kw = dict(
        timeout=timeout,
        username=username,
        key=key,
        key_pem=key_pem,
        password=password,
        port=port,
    )
    if port == 23:
        out = await ssh_run_ok(
            settings, ip, f"sha256sum {quoted}", **run_kw
        )
    else:
        try:
            out = await ssh_run_ok(
                settings, ip, f"sha256sum -- {quoted}", **run_kw
            )
        except DockerControlError:
            out = await ssh_run_ok(
                settings,
                ip,
                f"shasum -a 256 -- {quoted}",
                **run_kw,
            )
    digest = out.strip().split()[0] if out.strip() else ""
    if len(digest) != 64:
        raise DockerControlError(
            f"SHA256 auf {ip} ungültig für {remote_path}",
            status_code=502,
        )
    return digest.lower()


def local_sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


async def local_run(cmd: str, *, timeout: float = 600.0) -> tuple[str, str, int]:
    # Backup scripts use bash features (pipefail); do not use dash /bin/sh.
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        executable="/bin/bash",
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise DockerControlError(
            f"Lokaler Befehl-Timeout: {cmd[:80]}…",
            status_code=504,
        ) from exc
    stdout = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr = (stderr_b or b"").decode("utf-8", errors="replace")
    return stdout, stderr, int(proc.returncode or 0)


async def local_run_ok(cmd: str, *, timeout: float = 600.0) -> str:
    stdout, stderr, code = await local_run(cmd, timeout=timeout)
    if code != 0:
        detail = (stderr or stdout or "").strip() or f"exit {code}"
        raise DockerControlError(
            f"Lokaler Befehl fehlgeschlagen: {detail}",
            status_code=502,
        )
    return stdout


def shell_quote_list(paths: list[str]) -> str:
    return " ".join(shlex.quote(p) for p in paths)


async def ensure_remote_dir(
    settings: Settings, ip: str, path: str, *, timeout: float = 30.0
) -> None:
    await ssh_run_ok(settings, ip, f"mkdir -p -- {shlex.quote(path)}", timeout=timeout)
