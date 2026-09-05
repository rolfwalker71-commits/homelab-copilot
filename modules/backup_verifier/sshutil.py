"""SSH helpers for backup: long-running commands + SCP."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shlex
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import asyncssh

from app.config import Settings
from app.core.docker_control import DockerControlError, ssh_key_path
from app.core.locale import format_bytes

logger = logging.getLogger(__name__)

LogFn = Callable[[str], Awaitable[None]]
SftpProgressFn = Callable[[str, int], Awaitable[None]]

# Keepalive so idle long sessions are not dropped by NAT/firewall mid-command.
_SSH_KEEPALIVE_INTERVAL = 30
_SSH_KEEPALIVE_COUNT_MAX = 3
_SFTP_RETRIES = 4
_SFTP_RETRY_SLEEP = 2.0
_SFTP_PROGRESS_EVERY = 40
_RSYNC_MISSING = re.compile(
    r"command not found|kommando nicht gefunden|not installed|"
    r"connection unexpectedly closed",
    re.I,
)


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
    progress_label: str = "Archiv",
    job_label: str = "Archiv-Job",
) -> None:
    """Write script on target, start with nohup, poll status via short SSH calls.

    Status file values: ``running``, ``ok``, or ``failed:<exitcode>``.
    Avoids holding one SSH session open for multi-minute tar / apt jobs.
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
export HC_JOB_PROGRESS={shlex.quote(paths["progress"])}
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
                f"{job_label} Timeout nach {int(overall_timeout)}s "
                f"(Job zu lang oder Host langsam)",
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
            await log(f"{progress_label}: {progress}")

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
                f"{job_label} fehlgeschlagen ({status}): {detail[-500:]}",
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
                    f"{job_label} fehlgeschlagen ({status2})",
                    status_code=502,
                )
            raise DockerControlError(
                f"{job_label} beendet ohne Status (Prozess tot)",
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


def _sftp_connect_ctx(
    settings: Settings,
    ip: str,
    *,
    username: str | None = None,
    key: Path | None = None,
    key_pem: str | None = None,
    password: str | None = None,
    port: int | None = None,
):
    return asyncssh.connect(
        ip,
        **_ssh_connect_kwargs(
            settings,
            username=username,
            key=key,
            key_pem=key_pem,
            password=password,
            port=port,
        ),
    )


async def sftp_write_bytes(
    settings: Settings,
    ip: str,
    remote_path: str,
    data: bytes,
    *,
    mode: int = 0o600,
    username: str | None = None,
    key: Path | None = None,
    key_pem: str | None = None,
    password: str | None = None,
    port: int | None = None,
) -> None:
    """Write bytes via SFTP (avoids putting secrets on the SSH command line)."""
    parent = str(Path(remote_path).parent)
    try:
        async with _sftp_connect_ctx(
            settings,
            ip,
            username=username,
            key=key,
            key_pem=key_pem,
            password=password,
            port=port,
        ) as conn:
            async with conn.start_sftp_client() as sftp:
                if parent not in (".", "", "/"):
                    try:
                        await sftp.makedirs(parent, exist_ok=True)
                    except (OSError, asyncssh.SFTPError):
                        pass
                async with sftp.open(remote_path, "wb") as fh:
                    await fh.write(data)
                try:
                    await sftp.chmod(remote_path, mode)
                except (OSError, asyncssh.SFTPError):
                    pass
    except (OSError, asyncssh.Error) as exc:
        raise DockerControlError(
            format_ssh_failure(ip, exc, username=username),
            status_code=502,
        ) from exc


def _sftp_is_dir(attrs: object | None) -> bool:
    if attrs is None:
        return False
    typ = getattr(attrs, "type", None)
    dir_type = getattr(asyncssh, "FILEXFER_TYPE_DIRECTORY", 2)
    if typ is not None:
        return typ == dir_type
    perms = getattr(attrs, "permissions", None)
    if perms is None:
        return False
    return bool(int(perms) & 0o040000)


def _sftp_is_symlink(attrs: object | None) -> bool:
    if attrs is None:
        return False
    typ = getattr(attrs, "type", None)
    link_type = getattr(asyncssh, "FILEXFER_TYPE_SYMLINK", 3)
    if typ is not None:
        return typ == link_type
    perms = getattr(attrs, "permissions", None)
    if perms is None:
        return False
    return (int(perms) & 0o170000) == 0o120000


async def sftp_listdir(
    settings: Settings,
    ip: str,
    remote_dir: str,
    *,
    timeout: float = 60.0,
    username: str | None = None,
    key: Path | None = None,
    key_pem: str | None = None,
    password: str | None = None,
    port: int | None = None,
) -> list[dict]:
    """List one remote directory via SFTP (no file contents)."""
    remote = remote_dir.rstrip("/") or "/"

    async def _read() -> list[dict]:
        async with _sftp_connect_ctx(
            settings,
            ip,
            username=username,
            key=key,
            key_pem=key_pem,
            password=password,
            port=port,
        ) as conn:
            async with conn.start_sftp_client() as sftp:
                try:
                    entries = await sftp.readdir(remote)
                except (OSError, asyncssh.SFTPError) as exc:
                    text = str(exc).lower()
                    if "no such" in text or "not found" in text:
                        raise DockerControlError(
                            f"SFTP-Ordner nicht gefunden ({ip}:{remote})",
                            status_code=404,
                        ) from exc
                    if "permission" in text:
                        raise DockerControlError(
                            f"Keine Berechtigung für SFTP-Ordner ({ip}:{remote})",
                            status_code=403,
                        ) from exc
                    raise DockerControlError(
                        f"SFTP-Verzeichnis nicht lesbar ({ip}:{remote}): {exc}",
                        status_code=502,
                    ) from exc
                out: list[dict] = []
                for ent in entries:
                    name = ent.filename
                    if name in (".", ".."):
                        continue
                    rpath = f"{remote.rstrip('/')}/{name}"
                    attrs = ent.attrs
                    symlink = _sftp_is_symlink(attrs)
                    is_dir = _sftp_is_dir(attrs)
                    link_target = None
                    if symlink:
                        try:
                            link_target = await sftp.readlink(rpath)
                        except (OSError, asyncssh.SFTPError):
                            link_target = None
                    if not is_dir and not symlink:
                        try:
                            st = await sftp.stat(rpath)
                            is_dir = _sftp_is_dir(st)
                            attrs = st
                        except (OSError, asyncssh.SFTPError):
                            pass
                    size = getattr(attrs, "size", None)
                    mtime = getattr(attrs, "mtime", None)
                    out.append(
                        {
                            "name": name,
                            "path": rpath,
                            "is_dir": is_dir,
                            "symlink": symlink,
                            "link_target": link_target,
                            "size": int(size) if size is not None else None,
                            "mtime": float(mtime) if mtime is not None else None,
                        }
                    )
                return out

    try:
        return await asyncio.wait_for(_read(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise DockerControlError(
            f"SFTP-Timeout bei {ip}:{remote}",
            status_code=504,
        ) from exc
    except DockerControlError:
        raise
    except (OSError, asyncssh.Error) as exc:
        raise DockerControlError(
            format_ssh_failure(ip, exc, username=username),
            status_code=502,
        ) from exc


async def sftp_stat_file(
    settings: Settings,
    ip: str,
    remote_path: str,
    *,
    timeout: float = 30.0,
    username: str | None = None,
    key: Path | None = None,
    key_pem: str | None = None,
    password: str | None = None,
    port: int | None = None,
) -> dict:
    """Stat a remote file; raises if missing or a directory."""

    async def _stat() -> dict:
        async with _sftp_connect_ctx(
            settings,
            ip,
            username=username,
            key=key,
            key_pem=key_pem,
            password=password,
            port=port,
        ) as conn:
            async with conn.start_sftp_client() as sftp:
                try:
                    st = await sftp.stat(remote_path)
                except (OSError, asyncssh.SFTPError) as exc:
                    raise DockerControlError(
                        f"SFTP-Datei nicht gefunden ({ip}:{remote_path})",
                        status_code=404,
                    ) from exc
                if _sftp_is_dir(st):
                    raise DockerControlError(
                        "Nur Dateien können geladen werden.",
                        status_code=400,
                    )
                return {
                    "size": int(getattr(st, "size", 0) or 0),
                    "mtime": getattr(st, "mtime", None),
                }

    try:
        return await asyncio.wait_for(_stat(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise DockerControlError(
            f"SFTP-Timeout bei {ip}:{remote_path}",
            status_code=504,
        ) from exc
    except DockerControlError:
        raise
    except (OSError, asyncssh.Error) as exc:
        raise DockerControlError(
            format_ssh_failure(ip, exc, username=username),
            status_code=502,
        ) from exc


async def sftp_iter_file(
    settings: Settings,
    ip: str,
    remote_path: str,
    *,
    chunk_size: int = 65536,
    username: str | None = None,
    key: Path | None = None,
    key_pem: str | None = None,
    password: str | None = None,
    port: int | None = None,
):
    """Yield file bytes via SFTP (connection held for the generator lifetime)."""
    try:
        async with _sftp_connect_ctx(
            settings,
            ip,
            username=username,
            key=key,
            key_pem=key_pem,
            password=password,
            port=port,
        ) as conn:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(remote_path, "rb") as fh:
                    while True:
                        chunk = await fh.read(chunk_size)
                        if not chunk:
                            break
                        yield chunk
    except DockerControlError:
        raise
    except (OSError, asyncssh.Error) as exc:
        raise DockerControlError(
            format_ssh_failure(ip, exc, username=username),
            status_code=502,
        ) from exc


class _SftpSession:
    """Reconnectable SFTP client for long restic-repo mirrors."""

    def __init__(
        self,
        settings: Settings,
        ip: str,
        *,
        username: str | None = None,
        key: Path | None = None,
        key_pem: str | None = None,
        password: str | None = None,
        port: int | None = None,
    ) -> None:
        self._settings = settings
        self._ip = ip
        self._auth = {
            "username": username,
            "key": key,
            "key_pem": key_pem,
            "password": password,
            "port": port,
        }
        self.conn: asyncssh.SSHClientConnection | None = None
        self.sftp: asyncssh.SFTPClient | None = None

    async def open(self) -> None:
        await self.close()
        self.conn = await asyncssh.connect(
            self._ip,
            **_ssh_connect_kwargs(self._settings, **self._auth),
        )
        self.sftp = await self.conn.start_sftp_client()

    async def close(self) -> None:
        sftp, conn = self.sftp, self.conn
        self.sftp = None
        self.conn = None
        if sftp is not None:
            try:
                sftp.exit()
            except Exception:
                pass
        if conn is not None:
            conn.close()
            try:
                await conn.wait_closed()
            except Exception:
                pass

    def client(self) -> asyncssh.SFTPClient:
        if self.sftp is None:
            raise DockerControlError(
                f"SFTP-Sitzung zu {self._ip} ist geschlossen.",
                status_code=502,
            )
        return self.sftp


async def _sftp_mkdirs(sftp, remote: str) -> None:
    """Create remote dirs one level at a time (Hetzner Storage Box SFTP)."""
    remote = remote.rstrip("/")
    if not remote or remote == "/":
        return
    try:
        st = await sftp.stat(remote)
        if _sftp_is_dir(st):
            return
    except (OSError, asyncssh.SFTPError):
        pass
    parent = str(Path(remote).parent).replace("\\", "/")
    if parent not in (".", "", "/"):
        await _sftp_mkdirs(sftp, parent)
    try:
        await sftp.mkdir(remote)
    except (OSError, asyncssh.SFTPError):
        pass


def _rsync_missing_error(detail: str) -> bool:
    return bool(_RSYNC_MISSING.search(detail or ""))


async def _remote_has_rsync(
    settings: Settings,
    ip: str,
    *,
    username: str | None = None,
    port: int | None = None,
) -> bool:
    out, err, code = await ssh_run(
        settings,
        ip,
        "command -v rsync",
        timeout=15,
        username=username,
        port=port,
    )
    if code == 0 and (out or "").strip():
        return True
    text = f"{out}\n{err}"
    if _rsync_missing_error(text) or code != 0:
        return False
    return False


async def _sftp_call(session: _SftpSession, op, *, timeout: float, retries: int = _SFTP_RETRIES):
    """Run an SFTP coroutine with reconnect + per-call timeout."""
    last: BaseException | None = None
    for attempt in range(retries):
        try:
            return await asyncio.wait_for(op(session.client()), timeout=timeout)
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
            last = exc
            if attempt + 1 >= retries:
                break
            await asyncio.sleep(_SFTP_RETRY_SLEEP * (attempt + 1))
            try:
                await session.open()
            except (OSError, asyncssh.Error) as open_exc:
                last = open_exc
    assert last is not None
    raise last


def _count_local_files(local_dir: Path, skip: frozenset[str]) -> tuple[int, int]:
    """Return (file_count, total_bytes) under local_dir, honoring skip names."""
    files = 0
    nbytes = 0
    if not local_dir.is_dir():
        return 0, 0
    for item in local_dir.rglob("*"):
        try:
            rel = item.relative_to(local_dir)
        except ValueError:
            continue
        if any(part in skip for part in rel.parts):
            continue
        if not item.is_file():
            continue
        files += 1
        try:
            nbytes += int(item.stat().st_size)
        except OSError:
            pass
    return files, nbytes


def _sftp_progress_prefix(label: str) -> str:
    name = (label or "").strip()
    if not name or name.lower() == "sftp":
        return "SFTP"
    return f"SFTP {name}"


def _sftp_file_percent(stats: dict[str, int]) -> int:
    done = int(stats.get("copied", 0)) + int(stats.get("skipped", 0))
    total = int(stats.get("files_total", 0) or 0)
    if total <= 0:
        return 0
    shown = max(total, done)
    return min(100, int(round(100.0 * done / shown)))


def _format_sftp_progress(stats: dict[str, int], *, label: str) -> str:
    done = int(stats.get("copied", 0)) + int(stats.get("skipped", 0))
    total = int(stats.get("files_total", 0) or 0)
    bytes_done = int(stats.get("bytes_done", 0) or 0)
    bytes_total = int(stats.get("bytes_total", 0) or 0)
    if total > 0:
        shown = max(total, done)
        pct = _sftp_file_percent(stats)
        files = f"{done}/{shown} Dateien ({pct} %)"
    else:
        files = f"{done} Dateien"
    bits = [
        f"{_sftp_progress_prefix(label)}: {files}",
        f"{int(stats.get('copied', 0))} neu",
        f"{int(stats.get('deleted', 0))} entfernt",
    ]
    if bytes_total > 0:
        bits.append(f"{format_bytes(bytes_done)} / {format_bytes(bytes_total)}")
    elif bytes_done > 0:
        bits.append(format_bytes(bytes_done))
    return " · ".join(bits)


async def _sftp_count_remote_files(
    session: _SftpSession,
    remote: str,
    skip: frozenset[str],
    timeout: float,
) -> tuple[int, int]:
    """Return (file_count, total_bytes) via SFTP readdir, honoring skip names."""
    files = 0
    nbytes = 0
    unknown = getattr(asyncssh, "FILEXFER_TYPE_UNKNOWN", 0)

    async def _walk(path: str) -> None:
        nonlocal files, nbytes

        async def _readdir(sftp):
            return await sftp.readdir(path)

        try:
            entries = await _sftp_call(session, _readdir, timeout=min(120.0, timeout))
        except (OSError, asyncssh.SFTPError, asyncio.TimeoutError):
            return
        for ent in entries:
            name = ent.filename
            if name in (".", "..") or name in skip:
                continue
            rpath = f"{path.rstrip('/')}/{name}"
            attrs = ent.attrs
            is_dir = _sftp_is_dir(attrs)
            typ = getattr(attrs, "type", None) if attrs is not None else None
            if typ is None or typ == unknown:
                try:
                    st = await _sftp_call(
                        session,
                        lambda sftp, p=rpath: sftp.stat(p),
                        timeout=min(60.0, timeout),
                    )
                    is_dir = _sftp_is_dir(st)
                    attrs = st
                except (OSError, asyncssh.SFTPError, asyncio.TimeoutError):
                    is_dir = False
            if is_dir:
                await _walk(rpath)
                continue
            files += 1
            nbytes += int(getattr(attrs, "size", 0) or 0)

    await _walk(remote.rstrip("/"))
    return files, nbytes


async def _sftp_note_progress(
    log: LogFn | None,
    stats: dict[str, int],
    *,
    label: str = "SFTP",
    force: bool = False,
    on_progress: SftpProgressFn | None = None,
) -> None:
    n = stats["copied"] + stats["skipped"]
    if log is None and on_progress is None:
        return
    if not force and (n == 0 or n % _SFTP_PROGRESS_EVERY != 0):
        return
    msg = _format_sftp_progress(stats, label=label)
    if log is not None:
        await log(msg)
    if on_progress is not None:
        await on_progress(msg, _sftp_file_percent(stats))


async def sftp_mirror_get(
    settings: Settings,
    ip: str,
    remote_dir: str,
    local_dir: Path,
    *,
    timeout: float = 3600.0,
    exclude: frozenset[str] | None = None,
    delete_extra: bool = True,
    username: str | None = None,
    key: Path | None = None,
    key_pem: str | None = None,
    password: str | None = None,
    port: int | None = None,
    log: LogFn | None = None,
    label: str = "SFTP",
    on_progress: SftpProgressFn | None = None,
) -> dict[str, int]:
    """Mirror remote directory → local (skip same-size files).

    ``timeout`` is per file / stall; overall wall clock is 4× that so a
    first 15 GB restic repo can finish over a slow SFTP link.
    """
    skip = exclude or frozenset({"locks"})
    local_dir.mkdir(parents=True, exist_ok=True)
    stats = {
        "copied": 0,
        "skipped": 0,
        "deleted": 0,
        "bytes_done": 0,
        "files_total": 0,
        "bytes_total": 0,
    }
    started = time.monotonic()
    overall = max(float(timeout) * 4.0, float(timeout))

    def _budget() -> None:
        if time.monotonic() - started > overall:
            raise DockerControlError(
                f"SFTP-Sync-Timeout von {ip}:{remote_dir}",
                status_code=504,
            )

    session = _SftpSession(
        settings,
        ip,
        username=username,
        key=key,
        key_pem=key_pem,
        password=password,
        port=port,
    )

    async def _walk(remote: str, local: Path) -> None:
        _budget()
        local.mkdir(parents=True, exist_ok=True)

        async def _readdir(sftp):
            return await sftp.readdir(remote)

        try:
            entries = await _sftp_call(session, _readdir, timeout=min(120.0, timeout))
        except (OSError, asyncssh.SFTPError, asyncio.TimeoutError) as exc:
            raise DockerControlError(
                f"SFTP-Verzeichnis nicht lesbar ({ip}:{remote}): {exc}",
                status_code=502,
            ) from exc
        remote_names: set[str] = set()
        for ent in entries:
            name = ent.filename
            if name in (".", "..") or name in skip:
                continue
            remote_names.add(name)
            rpath = f"{remote.rstrip('/')}/{name}"
            lpath = local / name
            attrs = ent.attrs
            is_dir = _sftp_is_dir(attrs)
            if not is_dir:
                try:
                    st = await _sftp_call(
                        session, lambda sftp, p=rpath: sftp.stat(p), timeout=min(60.0, timeout)
                    )
                    is_dir = _sftp_is_dir(st)
                except (OSError, asyncssh.SFTPError, asyncio.TimeoutError):
                    is_dir = False
            if is_dir:
                await _walk(rpath, lpath)
                continue
            rsize = 0
            try:
                rst = await _sftp_call(
                    session, lambda sftp, p=rpath: sftp.stat(p), timeout=min(60.0, timeout)
                )
                rsize = int(getattr(rst, "size", 0) or 0)
                if lpath.is_file() and lpath.stat().st_size == rsize:
                    stats["skipped"] += 1
                    stats["bytes_done"] += rsize
                    await _sftp_note_progress(
                        log, stats, label=label, on_progress=on_progress
                    )
                    continue
            except (OSError, asyncssh.SFTPError, asyncio.TimeoutError):
                pass
            _budget()
            await _sftp_call(
                session,
                lambda sftp, src=rpath, dst=str(lpath): sftp.get(src, dst),
                timeout=timeout,
            )
            if rsize <= 0:
                try:
                    rsize = int(lpath.stat().st_size)
                except OSError:
                    rsize = 0
            stats["copied"] += 1
            stats["bytes_done"] += rsize
            await _sftp_note_progress(
                log, stats, label=label, on_progress=on_progress
            )
        if delete_extra:
            for child in list(local.iterdir()):
                if child.name in skip or child.name in remote_names:
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
                stats["deleted"] += 1

    try:
        await session.open()
        try:
            nfiles, nbytes = await _sftp_count_remote_files(
                session, remote_dir.rstrip("/"), skip, timeout
            )
            stats["files_total"] = nfiles
            stats["bytes_total"] = nbytes
        except (OSError, asyncssh.Error, asyncio.TimeoutError):
            pass
        await _sftp_note_progress(
            log, stats, label=label, force=True, on_progress=on_progress
        )
        await _walk(remote_dir.rstrip("/"), local_dir)
        await _sftp_note_progress(
            log, stats, label=label, force=True, on_progress=on_progress
        )
    except asyncio.TimeoutError as exc:
        raise DockerControlError(
            f"SFTP-Sync-Timeout von {ip}:{remote_dir}",
            status_code=504,
        ) from exc
    except DockerControlError:
        raise
    except (OSError, asyncssh.Error) as exc:
        raise DockerControlError(
            format_ssh_failure(ip, exc, username=username),
            status_code=502,
        ) from exc
    finally:
        await session.close()
    return stats


async def sftp_mirror_put(
    settings: Settings,
    ip: str,
    local_dir: Path,
    remote_dir: str,
    *,
    timeout: float = 3600.0,
    exclude: frozenset[str] | None = None,
    delete_extra: bool = True,
    username: str | None = None,
    key: Path | None = None,
    key_pem: str | None = None,
    password: str | None = None,
    port: int | None = None,
    log: LogFn | None = None,
    label: str = "SFTP",
    on_progress: SftpProgressFn | None = None,
) -> dict[str, int]:
    """Mirror local directory → remote (skip same-size files).

    ``timeout`` is per file / stall; overall wall clock is 4× that so a
    first 15 GB restic repo can finish over a slow Storage-Box link.
    """
    skip = exclude or frozenset({"locks"})
    if not local_dir.is_dir():
        raise DockerControlError(
            f"Lokales Verzeichnis fehlt: {local_dir}",
            status_code=400,
        )
    nfiles, nbytes = _count_local_files(local_dir, skip)
    stats = {
        "copied": 0,
        "skipped": 0,
        "deleted": 0,
        "bytes_done": 0,
        "files_total": nfiles,
        "bytes_total": nbytes,
    }
    started = time.monotonic()
    overall = max(float(timeout) * 4.0, float(timeout))

    def _budget() -> None:
        if time.monotonic() - started > overall:
            raise DockerControlError(
                f"SFTP-Sync-Timeout nach {ip}:{remote_dir}",
                status_code=504,
            )

    session = _SftpSession(
        settings,
        ip,
        username=username,
        key=key,
        key_pem=key_pem,
        password=password,
        port=port,
    )

    async def _rm_tree(remote: str) -> None:
        try:
            entries = await _sftp_call(
                session, lambda sftp, p=remote: sftp.readdir(p), timeout=min(120.0, timeout)
            )
        except (OSError, asyncssh.SFTPError, asyncio.TimeoutError):
            return
        for ent in entries:
            name = ent.filename
            if name in (".", ".."):
                continue
            rpath = f"{remote.rstrip('/')}/{name}"
            attrs = ent.attrs
            is_dir = _sftp_is_dir(attrs)
            if is_dir:
                await _rm_tree(rpath)
                try:
                    await _sftp_call(
                        session, lambda sftp, p=rpath: sftp.rmdir(p), timeout=min(60.0, timeout)
                    )
                except (OSError, asyncssh.SFTPError, asyncio.TimeoutError):
                    pass
            else:
                try:
                    await _sftp_call(
                        session, lambda sftp, p=rpath: sftp.remove(p), timeout=min(60.0, timeout)
                    )
                except (OSError, asyncssh.SFTPError, asyncio.TimeoutError):
                    pass

    async def _walk(local: Path, remote: str) -> None:
        _budget()
        await _sftp_call(
            session, lambda sftp, p=remote: _sftp_mkdirs(sftp, p), timeout=min(120.0, timeout)
        )
        remote_names: set[str] = set()
        try:
            entries = await _sftp_call(
                session, lambda sftp, p=remote: sftp.readdir(p), timeout=min(120.0, timeout)
            )
            for ent in entries:
                if ent.filename not in (".", ".."):
                    remote_names.add(ent.filename)
        except (OSError, asyncssh.SFTPError, asyncio.TimeoutError):
            entries = []
        local_names: set[str] = set()
        for item in local.iterdir():
            if item.name in skip:
                continue
            local_names.add(item.name)
            rpath = f"{remote.rstrip('/')}/{item.name}"
            if item.is_dir():
                await _walk(item, rpath)
                continue
            try:
                lsize = int(item.stat().st_size)
            except OSError:
                lsize = 0
            try:
                rst = await _sftp_call(
                    session, lambda sftp, p=rpath: sftp.stat(p), timeout=min(60.0, timeout)
                )
                if int(getattr(rst, "size", 0) or 0) == lsize:
                    stats["skipped"] += 1
                    stats["bytes_done"] += lsize
                    await _sftp_note_progress(
                        log, stats, label=label, on_progress=on_progress
                    )
                    continue
            except (OSError, asyncssh.SFTPError, asyncio.TimeoutError):
                pass
            _budget()
            await _sftp_call(
                session,
                lambda sftp, src=str(item), dst=rpath: sftp.put(src, dst),
                timeout=timeout,
            )
            stats["copied"] += 1
            stats["bytes_done"] += lsize
            await _sftp_note_progress(
                log, stats, label=label, on_progress=on_progress
            )
        if delete_extra:
            for name in remote_names - local_names:
                if name in skip:
                    continue
                rpath = f"{remote.rstrip('/')}/{name}"
                await _rm_tree(rpath)
                try:
                    await _sftp_call(
                        session, lambda sftp, p=rpath: sftp.remove(p), timeout=min(60.0, timeout)
                    )
                except (OSError, asyncssh.SFTPError, asyncio.TimeoutError):
                    try:
                        await _sftp_call(
                            session, lambda sftp, p=rpath: sftp.rmdir(p), timeout=min(60.0, timeout)
                        )
                    except (OSError, asyncssh.SFTPError, asyncio.TimeoutError):
                        pass
                stats["deleted"] += 1

    try:
        # Count is local — emit 0/N before the (often slow) Storage-Box connect.
        await _sftp_note_progress(
            log, stats, label=label, force=True, on_progress=on_progress
        )
        await session.open()
        await _walk(local_dir, remote_dir.rstrip("/"))
        await _sftp_note_progress(
            log, stats, label=label, force=True, on_progress=on_progress
        )
    except asyncio.TimeoutError as exc:
        raise DockerControlError(
            f"SFTP-Sync-Timeout nach {ip}:{remote_dir}",
            status_code=504,
        ) from exc
    except DockerControlError:
        raise
    except (OSError, asyncssh.Error) as exc:
        raise DockerControlError(
            format_ssh_failure(ip, exc, username=username),
            status_code=502,
        ) from exc
    finally:
        await session.close()
    return stats


def _rsync_ssh_cmd(settings: Settings, *, username: str | None, port: int | None) -> tuple[str, str, int]:
    key = ssh_key_path(settings)
    user = username or settings.docker_ssh_user
    ssh_port = port or settings.docker_ssh_port
    ssh_cmd = (
        f"ssh -i {shlex.quote(str(key))} -p {int(ssh_port)} "
        f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o BatchMode=yes -o LogLevel=ERROR"
    )
    return ssh_cmd, user, int(ssh_port)


async def rsync_pull(
    settings: Settings,
    ip: str,
    remote_dir: str,
    local_dir: Path,
    *,
    timeout: float = 3600.0,
    exclude: tuple[str, ...] = ("locks",),
    username: str | None = None,
    port: int | None = None,
) -> bool:
    """Pull remote→local with rsync if both sides have it. False = use SFTP."""
    which, _, code = await local_run("command -v rsync", timeout=10)
    if code != 0 or not (which or "").strip():
        return False
    if not await _remote_has_rsync(settings, ip, username=username, port=port):
        return False
    local_dir.mkdir(parents=True, exist_ok=True)
    ssh_cmd, user, _ = _rsync_ssh_cmd(settings, username=username, port=port)
    excludes = " ".join(f"--exclude {shlex.quote(x)}" for x in exclude)
    src = f"{user}@{ip}:{remote_dir.rstrip('/')}/"
    cmd = (
        f"rsync -a --delete {excludes} "
        f"-e {shlex.quote(ssh_cmd)} "
        f"{shlex.quote(src)} {shlex.quote(str(local_dir) + '/')}"
    )
    try:
        await local_run_ok(cmd, timeout=timeout)
    except DockerControlError as exc:
        if _rsync_missing_error(exc.message):
            return False
        raise
    return True


async def rsync_push(
    settings: Settings,
    ip: str,
    local_dir: Path,
    remote_dir: str,
    *,
    timeout: float = 3600.0,
    exclude: tuple[str, ...] = ("locks",),
    username: str | None = None,
    port: int | None = None,
) -> bool:
    """Push local→remote with rsync if both sides have it. False = use SFTP."""
    which, _, code = await local_run("command -v rsync", timeout=10)
    if code != 0 or not (which or "").strip():
        return False
    if not await _remote_has_rsync(settings, ip, username=username, port=port):
        return False
    if not local_dir.is_dir():
        raise DockerControlError(
            f"Lokales Verzeichnis fehlt: {local_dir}",
            status_code=400,
        )
    ssh_cmd, user, _ = _rsync_ssh_cmd(settings, username=username, port=port)
    excludes = " ".join(f"--exclude {shlex.quote(x)}" for x in exclude)
    dest = f"{user}@{ip}:{remote_dir.rstrip('/')}/"
    cmd = (
        f"rsync -a --delete {excludes} "
        f"-e {shlex.quote(ssh_cmd)} "
        f"{shlex.quote(str(local_dir) + '/')} {shlex.quote(dest)}"
    )
    try:
        await local_run_ok(cmd, timeout=timeout)
    except DockerControlError as exc:
        if _rsync_missing_error(exc.message):
            return False
        raise
    return True
