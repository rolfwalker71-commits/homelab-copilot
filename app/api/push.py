"""Web Push subscribe / unsubscribe / public VAPID key."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.app_store import AppStore
from app.core.locale import format_de, now_berlin
from app.core.push import ensure_vapid_keys

router = APIRouter(prefix="/push", tags=["push"])


class PushKeys(BaseModel):
    p256dh: str = Field(..., min_length=1)
    auth: str = Field(..., min_length=1)


class PushSubscribePayload(BaseModel):
    endpoint: str = Field(..., min_length=8)
    keys: PushKeys


class PushUnsubscribePayload(BaseModel):
    endpoint: str = Field(..., min_length=8)


def _store(request: Request) -> AppStore:
    store = getattr(request.app.state, "app_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Push-Store nicht bereit.")
    return store


@router.get("/vapid-public-key")
async def vapid_public_key(request: Request) -> dict[str, Any]:
    store = _store(request)
    keys = await ensure_vapid_keys(store)
    return {
        "publicKey": keys["public_key"],
        "subject": keys["subject"],
        "time": format_de(now_berlin()),
    }


@router.get("/status")
async def push_status(request: Request) -> dict[str, Any]:
    store = _store(request)
    keys = await ensure_vapid_keys(store)
    count = await store.push_subscription_count()
    return {
        "configured": bool(keys.get("public_key")),
        "subscription_count": count,
        "time": format_de(now_berlin()),
    }


@router.post("/subscribe")
async def push_subscribe(
    payload: PushSubscribePayload, request: Request
) -> dict[str, Any]:
    store = _store(request)
    await ensure_vapid_keys(store)
    ua = (request.headers.get("user-agent") or "")[:500]
    await store.upsert_push_subscription(
        endpoint=payload.endpoint.strip(),
        p256dh=payload.keys.p256dh.strip(),
        auth=payload.keys.auth.strip(),
        user_agent=ua,
    )
    return {
        "ok": True,
        "message": "Benachrichtigungen aktiviert.",
        "time": format_de(now_berlin()),
    }


@router.post("/unsubscribe")
async def push_unsubscribe(payload: PushUnsubscribePayload, request: Request) -> dict[str, Any]:
    store = _store(request)
    await store.delete_push_subscription(payload.endpoint.strip())
    return {
        "ok": True,
        "message": "Benachrichtigungen deaktiviert.",
        "time": format_de(now_berlin()),
    }
