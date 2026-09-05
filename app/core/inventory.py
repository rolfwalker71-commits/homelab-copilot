"""SQLite inventory: notes, extra tags, and links per topology entity."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from app.core.locale import format_de, iso_utc, now_berlin

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entity_docs (
    entity_id TEXT PRIMARY KEY,
    notes TEXT NOT NULL DEFAULT '',
    extra_tags_json TEXT NOT NULL DEFAULT '[]',
    links_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT,
    updated_at_iso TEXT
);
"""


class InventoryStore:
    """Per-entity notes / extra tags / links under DATA_DIR."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    def _require(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("InventoryStore nicht verbunden")
        return self._db

    @staticmethod
    def _parse_tags(raw: Any) -> list[str]:
        if isinstance(raw, list):
            parts = [str(x).strip() for x in raw]
        elif isinstance(raw, str):
            parts = [p.strip() for p in raw.replace(",", ";").split(";")]
        else:
            parts = []
        seen: set[str] = set()
        out: list[str] = []
        for p in parts:
            if not p:
                continue
            key = p.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        return out[:32]

    @staticmethod
    def _parse_links(raw: Any) -> list[dict[str, str]]:
        if not isinstance(raw, list):
            return []
        out: list[dict[str, str]] = []
        for item in raw[:40]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            label = str(item.get("label") or "").strip()
            if not url:
                continue
            if not (url.startswith("http://") or url.startswith("https://")):
                continue
            out.append({"url": url[:500], "label": (label or url)[:120]})
        return out

    def _row_dict(self, row: aiosqlite.Row | None, entity_id: str) -> dict[str, Any]:
        if row is None:
            return {
                "entity_id": entity_id,
                "notes": "",
                "extra_tags": [],
                "links": [],
                "updated_at": None,
                "updated_at_iso": None,
            }
        tags_raw = row["extra_tags_json"]
        links_raw = row["links_json"]
        try:
            tags = json.loads(tags_raw) if tags_raw else []
        except json.JSONDecodeError:
            tags = []
        try:
            links = json.loads(links_raw) if links_raw else []
        except json.JSONDecodeError:
            links = []
        return {
            "entity_id": row["entity_id"],
            "notes": row["notes"] or "",
            "extra_tags": self._parse_tags(tags),
            "links": self._parse_links(links),
            "updated_at": row["updated_at"],
            "updated_at_iso": row["updated_at_iso"],
        }

    async def get(self, entity_id: str) -> dict[str, Any]:
        entity_id = (entity_id or "").strip()
        db = self._require()
        async with db.execute(
            "SELECT * FROM entity_docs WHERE entity_id = ?", (entity_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._row_dict(row, entity_id)

    async def upsert(
        self,
        entity_id: str,
        *,
        notes: str = "",
        extra_tags: list[str] | str | None = None,
        links: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        entity_id = (entity_id or "").strip()
        if not entity_id:
            raise ValueError("entity_id fehlt.")
        tags = self._parse_tags(extra_tags or [])
        link_rows = self._parse_links(links or [])
        text = (notes or "").strip()[:8000]
        now = now_berlin()
        stamp, stamp_iso = format_de(now), iso_utc(now)
        db = self._require()
        await db.execute(
            """
            INSERT INTO entity_docs (
                entity_id, notes, extra_tags_json, links_json,
                updated_at, updated_at_iso
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_id) DO UPDATE SET
                notes = excluded.notes,
                extra_tags_json = excluded.extra_tags_json,
                links_json = excluded.links_json,
                updated_at = excluded.updated_at,
                updated_at_iso = excluded.updated_at_iso
            """,
            (
                entity_id,
                text,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(link_rows, ensure_ascii=False),
                stamp,
                stamp_iso,
            ),
        )
        await db.commit()
        return await self.get(entity_id)

    async def rehome(self, old_id: str, new_id: str) -> bool:
        """Copy Inventar/Doku to a new entity id if the destination is empty.

        The old row stays (not shown on the Hosts rail). Used when a guest is
        recreated under the same name with a new VMID.
        """
        old_id = (old_id or "").strip()
        new_id = (new_id or "").strip()
        if not old_id or not new_id or old_id == new_id:
            return False
        src = await self.get(old_id)
        if not (src.get("notes") or src.get("extra_tags") or src.get("links")):
            return False
        dest = await self.get(new_id)
        if dest.get("notes") or dest.get("extra_tags") or dest.get("links"):
            return False
        await self.upsert(
            new_id,
            notes=src.get("notes") or "",
            extra_tags=src.get("extra_tags") or [],
            links=src.get("links") or [],
        )
        return True


async def rehome_id_changes(
    store: InventoryStore | None,
    id_changes: list[tuple[str, str]] | tuple[tuple[str, str], ...],
) -> None:
    if store is None:
        return
    for old_id, new_id in id_changes:
        try:
            await store.rehome(old_id, new_id)
        except Exception:
            logger.exception("Inventar %s → %s nicht übernommen", old_id, new_id)
