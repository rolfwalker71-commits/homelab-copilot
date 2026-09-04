"""SQLite app settings under DATA_DIR — TOTP, VAPID, push subscriptions, secrets."""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any

import aiosqlite

from app.core.locale import format_de, iso_utc, now_berlin

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT,
    updated_at_iso TEXT
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    user_agent TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    created_at_iso TEXT NOT NULL
);
"""

# Well-known keys
KEY_COOKIE_SECRET = "cookie_secret"
KEY_TOTP_SECRET = "totp_secret"
KEY_TOTP_CONFIRMED = "totp_confirmed"
KEY_VAPID_PRIVATE = "vapid_private_key"
KEY_VAPID_PUBLIC = "vapid_public_key"
KEY_VAPID_SUBJECT = "vapid_subject"
KEY_PATCHER_LAST_DAILY = "patcher_last_daily_scan"


class AppStore:
    """Persistent key/value + push subscriptions (lives on named volume)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        await self.ensure_cookie_secret()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("AppStore nicht verbunden")
        return self._db

    async def get(self, key: str, default: str | None = None) -> str | None:
        cur = await self.db.execute("SELECT value FROM kv WHERE key = ?", (key,))
        row = await cur.fetchone()
        if row is None:
            return default
        return str(row["value"])

    async def set(self, key: str, value: str) -> None:
        now = now_berlin()
        await self.db.execute(
            """
            INSERT INTO kv (key, value, updated_at, updated_at_iso)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at,
                updated_at_iso = excluded.updated_at_iso
            """,
            (key, value, format_de(now), iso_utc(now)),
        )
        await self.db.commit()

    async def delete(self, key: str) -> None:
        await self.db.execute("DELETE FROM kv WHERE key = ?", (key,))
        await self.db.commit()

    async def ensure_cookie_secret(self) -> str:
        existing = await self.get(KEY_COOKIE_SECRET)
        if existing:
            return existing
        secret = secrets.token_urlsafe(48)
        await self.set(KEY_COOKIE_SECRET, secret)
        logger.info("Cookie-Secret neu erzeugt (DATA_DIR)")
        return secret

    # --- TOTP ---

    async def get_totp_secret(self) -> str | None:
        return await self.get(KEY_TOTP_SECRET)

    async def set_totp_secret(self, secret: str) -> None:
        await self.set(KEY_TOTP_SECRET, secret)

    async def is_totp_confirmed(self) -> bool:
        return (await self.get(KEY_TOTP_CONFIRMED, "0")) == "1"

    async def set_totp_confirmed(self, confirmed: bool = True) -> None:
        await self.set(KEY_TOTP_CONFIRMED, "1" if confirmed else "0")

    # --- VAPID ---

    async def get_vapid(self) -> dict[str, str | None]:
        return {
            "private_key": await self.get(KEY_VAPID_PRIVATE),
            "public_key": await self.get(KEY_VAPID_PUBLIC),
            "subject": await self.get(KEY_VAPID_SUBJECT),
        }

    async def set_vapid(
        self, *, private_key: str, public_key: str, subject: str
    ) -> None:
        await self.set(KEY_VAPID_PRIVATE, private_key)
        await self.set(KEY_VAPID_PUBLIC, public_key)
        await self.set(KEY_VAPID_SUBJECT, subject)

    # --- Push subscriptions ---

    async def upsert_push_subscription(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: str = "",
    ) -> None:
        now = now_berlin()
        await self.db.execute(
            """
            INSERT INTO push_subscriptions
                (endpoint, p256dh, auth, user_agent, created_at, created_at_iso)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh = excluded.p256dh,
                auth = excluded.auth,
                user_agent = excluded.user_agent
            """,
            (
                endpoint,
                p256dh,
                auth,
                user_agent[:500],
                format_de(now),
                iso_utc(now),
            ),
        )
        await self.db.commit()

    async def delete_push_subscription(self, endpoint: str) -> None:
        await self.db.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
        )
        await self.db.commit()

    async def list_push_subscriptions(self) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT endpoint, p256dh, auth, user_agent, created_at FROM push_subscriptions"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def push_subscription_count(self) -> int:
        cur = await self.db.execute("SELECT COUNT(*) AS n FROM push_subscriptions")
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # --- Patcher daily ---

    async def get_patcher_last_daily(self) -> str | None:
        return await self.get(KEY_PATCHER_LAST_DAILY)

    async def set_patcher_last_daily(self, iso_ts: str) -> None:
        await self.set(KEY_PATCHER_LAST_DAILY, iso_ts)
