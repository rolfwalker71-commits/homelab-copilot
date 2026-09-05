"""TOTP gate: secret persistence, signed session cookie, QR helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import time
from typing import Any
from urllib.parse import quote

import pyotp
import qrcode
from fastapi import Request, Response
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings, get_settings
from app.core.app_store import AppStore

logger = logging.getLogger(__name__)

COOKIE_NAME = "hlops_auth"
AUTH_PUBLIC_PREFIXES = (
    "/api/health",
    "/api/auth/",
    "/auth",
    "/static/",
    "/sw.js",
    "/manifest.webmanifest",
    "/offline",
    "/favicon.ico",
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def sign_payload(payload: dict[str, Any], secret: str) -> str:
    body = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64url(sig)}"


def verify_payload(token: str, secret: str) -> dict[str, Any] | None:
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    expect = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64url(expect), sig):
        return None
    try:
        data = json.loads(_b64url_decode(body))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    exp = data.get("exp")
    if not isinstance(exp, (int, float)) or time.time() > float(exp):
        return None
    return data


def create_session_token(store_secret: str, *, days: int) -> str:
    now = int(time.time())
    return sign_payload(
        {"v": 1, "iat": now, "exp": now + max(1, days) * 86400},
        store_secret,
    )


def cookie_secure_flag(request: Request, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    mode = (settings.totp_cookie_secure or "auto").lower()
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    # auto: honor reverse-proxy proto, else request scheme
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    scheme = forwarded or request.url.scheme
    return scheme == "https"


def set_auth_cookie(response: Response, request: Request, token: str) -> None:
    settings = get_settings()
    max_age = max(1, settings.totp_cookie_days) * 86400
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=cookie_secure_flag(request, settings),
        samesite="lax",
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


def request_has_valid_auth(request: Request, cookie_secret: str) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    return verify_payload(token, cookie_secret) is not None


async def ensure_totp_secret(store: AppStore) -> str:
    existing = await store.get_totp_secret()
    if existing:
        return existing
    secret = pyotp.random_base32()
    await store.set_totp_secret(secret)
    await store.set_totp_confirmed(False)
    logger.info("TOTP-Secret neu erzeugt (DATA_DIR)")
    return secret


def totp_uri(secret: str, *, issuer: str, account: str = "admin") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer)


def totp_qr_data_uri(secret: str, *, issuer: str, account: str = "admin") -> str:
    uri = totp_uri(secret, issuer=issuer, account=account)
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def verify_totp_code(secret: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return False
    totp = pyotp.TOTP(secret)
    return bool(totp.verify(code, valid_window=1))


def is_public_path(path: str) -> bool:
    if path in ("/sw.js", "/manifest.webmanifest", "/offline", "/favicon.ico"):
        return True
    for prefix in AUTH_PUBLIC_PREFIXES:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return True
    return False


def wants_html(request_headers: dict[str, str] | MutableHeaders, path: str) -> bool:
    accept = request_headers.get("accept", "")
    if "text/html" in accept:
        return True
    # Navigations and bare page paths without Accept
    if (
        path == "/"
        or path.startswith("/modules/")
        or path.startswith("/setup")
        or path.startswith("/mobile")
    ):
        return True
    return False


class TotpAuthMiddleware:
    """ASGI middleware: require valid TOTP session cookie for all non-public routes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or "/"
        if is_public_path(path):
            await self.app(scope, receive, send)
            return

        app_state = scope.get("app")
        store: AppStore | None = None
        cookie_secret: str | None = None
        if app_state is not None:
            store = getattr(app_state.state, "app_store", None)
            cookie_secret = getattr(app_state.state, "cookie_secret", None)

        if store is None or not cookie_secret:
            # Store not ready yet (startup) — allow health already exempted
            await self._reject(scope, receive, send, path, reason="auth_unavailable")
            return

        headers = MutableHeaders(scope=scope)
        cookie_header = headers.get("cookie", "")
        token = _parse_cookie(cookie_header, COOKIE_NAME)
        if token and verify_payload(token, cookie_secret):
            await self.app(scope, receive, send)
            return

        await self._reject(scope, receive, send, path, reason="unauthorized")

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        path: str,
        *,
        reason: str,
    ) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return

        headers = MutableHeaders(scope=scope)
        if wants_html(headers, path):
            next_q = quote(path)
            location = f"/auth/login?next={next_q}"
            await send(
                {
                    "type": "http.response.start",
                    "status": 302,
                    "headers": [
                        (b"location", location.encode()),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return

        body = json.dumps(
            {"detail": "Anmeldung erforderlich (TOTP).", "reason": reason}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _parse_cookie(header: str, name: str) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == name:
            return v.strip()
    return None
