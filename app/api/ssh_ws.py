"""In-browser SSH terminal over WebSocket (asyncssh + xterm.js client)."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any
from urllib.parse import unquote

import asyncssh
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import Settings, get_settings
from app.core import docker_control as docker_ctl
from app.core.models import TopologyEntity, TopologySnapshot

logger = logging.getLogger(__name__)

router = APIRouter()


def resolve_ssh_target(
    snapshot: TopologySnapshot | None, target_id: str
) -> TopologyEntity:
    """Resolve a topology guest or Proxmox node — never trusts client-supplied IPs."""
    target_id = unquote((target_id or "").strip())
    if not target_id:
        raise ValueError("Ziel-ID fehlt.")
    if snapshot is None:
        raise ValueError("Keine Topologie geladen — bitte zuerst Discovery ausführen.")

    for g in snapshot.guests:
        if g.id == target_id:
            if not g.ip_addresses:
                raise ValueError(f"Guest „{g.name}“ hat keine bekannte IP.")
            return g

    for n in snapshot.nodes:
        if n.id == target_id or (
            target_id.startswith("node:") and n.name == target_id.split(":", 1)[-1]
        ):
            if not n.ip_addresses:
                raise ValueError(
                    f"Node „{n.name}“ hat keine bekannte IP — "
                    "Discovery aktualisieren oder PROXMOX_HOST prüfen."
                )
            return n

    raise ValueError("Ziel nicht in der Topologie gefunden.")


# Backwards-compatible alias
resolve_guest = resolve_ssh_target


async def _ssh_connect(settings: Settings, ip: str) -> asyncssh.SSHClientConnection:
    key = docker_ctl.ssh_key_path(settings)
    connect_timeout = min(8.0, max(3.0, settings.docker_ssh_timeout + 4.0))
    return await asyncssh.connect(
        ip,
        port=settings.docker_ssh_port,
        username=settings.docker_ssh_user,
        client_keys=[str(key)],
        known_hosts=None,
        connect_timeout=connect_timeout,
        login_timeout=connect_timeout,
    )


def _handle_control(process: asyncssh.SSHClientProcess, text: str) -> bool:
    """Return True if ``text`` was a control JSON message (resize)."""
    stripped = text.strip()
    if not stripped.startswith("{") or '"type"' not in stripped:
        return False
    try:
        msg = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    if not isinstance(msg, dict) or msg.get("type") != "resize":
        return False
    try:
        cols = max(20, min(int(msg.get("cols", 80)), 500))
        rows = max(5, min(int(msg.get("rows", 24)), 200))
    except (TypeError, ValueError):
        return True
    with suppress(Exception):
        process.change_terminal_size(cols, rows)
    return True


@router.websocket("/ws/ssh/{guest_id:path}")
async def ssh_terminal_ws(websocket: WebSocket, guest_id: str) -> None:
    """Bidirectional SSH shell for a discovered guest or Proxmox node."""
    await websocket.accept()
    settings = get_settings()
    store = websocket.app.state.topology_store

    try:
        guest = resolve_ssh_target(store.snapshot, guest_id)
    except ValueError as exc:
        await websocket.send_text(f"\r\n\x1b[31m{exc}\x1b[0m\r\n")
        await websocket.close(code=4000)
        return

    if not docker_ctl.ssh_key_present(settings):
        path = docker_ctl.ssh_key_path(settings)
        await websocket.send_text(
            "\r\n\x1b[31mSSH-Schlüssel fehlt — Terminal nicht möglich. "
            f"Gleicher Key wie Docker-Discovery: {path}\x1b[0m\r\n"
        )
        await websocket.close(code=4001)
        return

    ip = guest.ip_addresses[0]
    cols, rows = 80, 24

    await websocket.send_text(
        f"\r\n\x1b[90mVerbinde mit {guest.name} ({ip}) als {settings.docker_ssh_user}…\x1b[0m\r\n"
    )

    conn: asyncssh.SSHClientConnection | None = None
    process: asyncssh.SSHClientProcess | None = None
    try:
        conn = await _ssh_connect(settings, ip)
        process = await conn.create_process(
            term_type="xterm-256color",
            term_size=(cols, rows),
        )
    except Exception as exc:
        logger.warning("SSH terminal connect %s (%s): %s", guest.id, ip, exc)
        await websocket.send_text(
            f"\r\n\x1b[31mSSH-Verbindung fehlgeschlagen: {exc}\x1b[0m\r\n"
        )
        await websocket.close(code=4002)
        if conn is not None:
            conn.close()
            with suppress(Exception):
                await conn.wait_closed()
        return

    await websocket.send_text(
        "\x1b[90mVerbunden — gleicher SSH-Key wie Docker-Discovery.\x1b[0m\r\n"
    )

    async def ssh_to_ws() -> None:
        assert process is not None
        try:
            while True:
                data = await process.stdout.read(4096)
                if not data:
                    break
                text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
                await websocket.send_text(text)
        except (asyncio.CancelledError, WebSocketDisconnect):
            raise
        except Exception as exc:
            logger.debug("ssh_to_ws ended: %s", exc)

    async def ws_to_ssh() -> None:
        assert process is not None
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                text = message.get("text")
                raw = message.get("bytes")
                if raw is not None and text is None:
                    text = raw.decode("utf-8", errors="replace")
                if text is None:
                    continue
                if _handle_control(process, text):
                    continue
                process.stdin.write(text)
        except (asyncio.CancelledError, WebSocketDisconnect):
            raise
        except Exception as exc:
            logger.debug("ws_to_ssh ended: %s", exc)

    reader = asyncio.create_task(ssh_to_ws(), name="ssh-to-ws")
    writer = asyncio.create_task(ws_to_ssh(), name="ws-to-ssh")
    try:
        _done, pending = await asyncio.wait(
            {reader, writer},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        if process is not None:
            with suppress(Exception):
                process.close()
                await process.wait_closed()
        if conn is not None:
            with suppress(Exception):
                conn.close()
                await conn.wait_closed()
        with suppress(Exception):
            await websocket.close()
