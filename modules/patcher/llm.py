"""OpenAI-compatible LLM summary for patch scans."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from patcher.config import PatcherSettings, get_patcher_settings

logger = logging.getLogger(__name__)


class LlmError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def summarize_scan(
    *,
    target_name: str,
    distro: str | None,
    pm: str | None,
    summary: dict[str, Any],
    packages: list[dict[str, Any]],
    settings: PatcherSettings | None = None,
) -> str:
    settings = settings or get_patcher_settings()
    if not settings.llm_configured:
        raise LlmError("Kein PATCHER_LLM_API_KEY gesetzt.")

    top = packages[:25]
    lines = []
    for p in top:
        lines.append(
            f"- {p.get('name')}: {p.get('current') or '?'} → {p.get('candidate') or '?'} "
            f"[{p.get('priority') or 'normal'}]"
        )
    pkg_block = "\n".join(lines) or "(keine Pakete)"

    user_prompt = (
        f"Host: {target_name}\n"
        f"Distro: {distro or 'unbekannt'}\n"
        f"Paketmanager: {pm or 'unbekannt'}\n"
        f"Ausstehend: {summary.get('total', len(packages))} "
        f"(Security: {summary.get('security', 0)}, Normal: {summary.get('normal', 0)})\n"
        f"Reboot nötig (Flag): {summary.get('reboot_required', False)}\n\n"
        f"Pakete:\n{pkg_block}\n\n"
        "Schreibe auf Deutsch eine kurze Einschätzung (max. 8 Sätze): "
        "Risiko, empfohlene Reihenfolge (Security zuerst?), ob Reboot wahrscheinlich, "
        "und was der Admin prüfen sollte. Keine Befehle ausführen — nur beraten. "
        "Kein Markdown-Codeblock."
    )

    url = settings.patcher_llm_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.patcher_llm_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.patcher_llm_model,
        "temperature": 0.3,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Du bist ein vorsichtiger Homelab-Sysadmin-Assistent für Linux-Patching. "
                    "Antworte knapp und klar auf Deutsch."
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=settings.patcher_llm_timeout) as client:
            resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise LlmError(f"LLM-Anfrage fehlgeschlagen: {exc}") from exc

    if resp.status_code >= 400:
        detail = (resp.text or "")[:400]
        raise LlmError(f"LLM HTTP {resp.status_code}: {detail}")

    try:
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LlmError("Unerwartete LLM-Antwort.") from exc

    text = (text or "").strip()
    if not text:
        raise LlmError("Leere LLM-Antwort.")
    return text[:4000]
