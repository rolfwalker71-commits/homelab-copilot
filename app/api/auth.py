"""Auth API: TOTP status, verify, logout, setup recovery."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.app_store import AppStore
from app.core.auth import (
    clear_auth_cookie,
    create_session_token,
    ensure_totp_secret,
    set_auth_cookie,
    totp_qr_data_uri,
    totp_uri,
    verify_totp_code,
)
from app.core.locale import format_de, now_berlin

router = APIRouter(prefix="/auth", tags=["auth"])


class TotpVerifyPayload(BaseModel):
    code: str = Field(..., min_length=6, max_length=12)


def _store(request: Request) -> AppStore:
    store = getattr(request.app.state, "app_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Auth-Store nicht bereit.")
    return store


@router.get("/status")
async def auth_status(request: Request) -> dict[str, Any]:
    store = _store(request)
    settings = get_settings()
    secret = await store.get_totp_secret()
    confirmed = await store.is_totp_confirmed()
    authenticated = bool(getattr(request.state, "authenticated", False))
    # Also check cookie via middleware path — for public status we re-check
    from app.core.auth import request_has_valid_auth

    cookie_secret = getattr(request.app.state, "cookie_secret", "") or ""
    authenticated = request_has_valid_auth(request, cookie_secret) if cookie_secret else False

    out: dict[str, Any] = {
        "authenticated": authenticated,
        "totp_initialized": bool(secret),
        "totp_confirmed": confirmed,
        "needs_setup": (not secret) or (not confirmed),
        "cookie_days": settings.totp_cookie_days,
        "issuer": settings.totp_issuer,
        "time": format_de(now_berlin()),
    }

    # First-run / unconfirmed: expose QR so login page can enroll
    if secret and not confirmed:
        out["qr_data_uri"] = totp_qr_data_uri(
            secret, issuer=settings.totp_issuer
        )
        out["otpauth_url"] = totp_uri(secret, issuer=settings.totp_issuer)
        out["secret"] = secret
    return out


@router.get("/setup")
async def auth_setup(request: Request) -> dict[str, Any]:
    """Recovery QR — only when already authenticated (or unconfirmed first-run)."""
    store = _store(request)
    settings = get_settings()
    from app.core.auth import request_has_valid_auth

    cookie_secret = getattr(request.app.state, "cookie_secret", "") or ""
    authenticated = request_has_valid_auth(request, cookie_secret)
    confirmed = await store.is_totp_confirmed()
    if confirmed and not authenticated:
        raise HTTPException(status_code=401, detail="Anmeldung erforderlich.")

    secret = await ensure_totp_secret(store)
    return {
        "ok": True,
        "issuer": settings.totp_issuer,
        "secret": secret,
        "otpauth_url": totp_uri(secret, issuer=settings.totp_issuer),
        "qr_data_uri": totp_qr_data_uri(secret, issuer=settings.totp_issuer),
        "totp_confirmed": confirmed,
        "time": format_de(now_berlin()),
    }


@router.post("/verify")
async def auth_verify(
    payload: TotpVerifyPayload, request: Request, response: Response
) -> dict[str, Any]:
    store = _store(request)
    settings = get_settings()
    secret = await ensure_totp_secret(store)
    if not verify_totp_code(secret, payload.code):
        raise HTTPException(status_code=401, detail="Ungültiger Code. Bitte erneut versuchen.")

    await store.set_totp_confirmed(True)
    cookie_secret = await store.ensure_cookie_secret()
    request.app.state.cookie_secret = cookie_secret
    token = create_session_token(cookie_secret, days=settings.totp_cookie_days)
    set_auth_cookie(response, request, token)
    return {
        "ok": True,
        "message": "Anmeldung erfolgreich.",
        "cookie_days": settings.totp_cookie_days,
        "time": format_de(now_berlin()),
    }


@router.post("/logout")
async def auth_logout(response: Response) -> dict[str, Any]:
    clear_auth_cookie(response)
    return {"ok": True, "message": "Abgemeldet.", "time": format_de(now_berlin())}
