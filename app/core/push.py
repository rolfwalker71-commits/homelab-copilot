"""Web Push: VAPID key management and notification delivery."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import Settings, get_settings
from app.core.app_store import KEY_VAPID_SUBJECT, AppStore

logger = logging.getLogger(__name__)


def _generate_vapid_keys() -> tuple[str, str]:
    """Return (private_key_pem, public_key_urlsafe_uncompressed)."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = base64.urlsafe_b64encode(public_raw).rstrip(b"=").decode("ascii")
    return private_pem, public_b64


async def ensure_vapid_keys(
    store: AppStore, settings: Settings | None = None
) -> dict[str, str]:
    """Ensure VAPID keys exist (env override or generate into DATA_DIR DB)."""
    settings = settings or get_settings()
    env_priv = (settings.vapid_private_key or "").strip()
    env_pub = (settings.vapid_public_key or "").strip()
    subject = (settings.vapid_subject or "mailto:admin@localhost").strip()

    if env_priv and env_pub:
        existing = await store.get_vapid()
        if (
            existing.get("private_key") != env_priv
            or existing.get("public_key") != env_pub
            or existing.get("subject") != subject
        ):
            await store.set_vapid(
                private_key=env_priv, public_key=env_pub, subject=subject
            )
            logger.info("VAPID-Keys aus Umgebungsvariablen übernommen")
        return {"private_key": env_priv, "public_key": env_pub, "subject": subject}

    existing = await store.get_vapid()
    if existing.get("private_key") and existing.get("public_key"):
        if not existing.get("subject"):
            await store.set(KEY_VAPID_SUBJECT, subject)
        return {
            "private_key": existing["private_key"] or "",
            "public_key": existing["public_key"] or "",
            "subject": existing.get("subject") or subject,
        }

    private, public = _generate_vapid_keys()
    await store.set_vapid(private_key=private, public_key=public, subject=subject)
    logger.info("VAPID-Schlüsselpaar neu erzeugt und in DATA_DIR gespeichert")
    return {"private_key": private, "public_key": public, "subject": subject}


async def send_push_to_all(
    store: AppStore,
    *,
    title: str,
    body: str,
    url: str = "/modules/patcher",
    tag: str = "homelab-ops",
) -> dict[str, Any]:
    """Send a Web Push notification to all stored subscriptions."""
    settings = get_settings()
    keys = await ensure_vapid_keys(store, settings)
    subs = await store.list_push_subscriptions()
    if not subs:
        return {"ok": True, "sent": 0, "failed": 0, "removed": 0}

    try:
        from pywebpush import WebPusher, WebPushException
    except ImportError:
        logger.error("pywebpush nicht installiert")
        return {"ok": False, "error": "pywebpush missing", "sent": 0, "failed": 0}

    payload = json.dumps(
        {"title": title, "body": body, "url": url, "tag": tag},
        ensure_ascii=False,
    )
    claims = {"sub": keys["subject"]}
    sent = 0
    failed = 0
    removed = 0

    for sub in subs:
        subscription_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        try:
            WebPusher(subscription_info).send(
                data=payload,
                vapid_private_key=keys["private_key"],
                vapid_claims=claims,
                ttl=86400,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning("WebPush fehlgeschlagen (%s): %s", status, exc)
            failed += 1
            if status in (404, 410):
                await store.delete_push_subscription(sub["endpoint"])
                removed += 1
        except Exception:
            logger.exception("WebPush unerwarteter Fehler")
            failed += 1

    return {"ok": True, "sent": sent, "failed": failed, "removed": removed}
