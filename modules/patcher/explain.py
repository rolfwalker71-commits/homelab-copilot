"""Rule-based German job explanations. LLM is optional enrichment only."""

from __future__ import annotations

import logging
import re
from typing import Any

from ops_agent.actor import agent_phrase, by_agent, is_via_agent
from patcher.config import PatcherSettings, get_patcher_settings
from patcher.llm import LlmError

logger = logging.getLogger(__name__)

_SECRET_HINT = re.compile(
    r"(api[_-]?key|secret|password|token|private[_-]?key|\.env\b|begin [a-z ]*private key)",
    re.I,
)


def _clean(text: str, *, max_len: int = 400) -> str:
    raw = " ".join((text or "").split())
    if _SECRET_HINT.search(raw):
        return "(Details gekürzt — keine Geheimnisse in der Erklärung.)"
    if len(raw) <= max_len:
        return raw
    return raw[: max_len - 1].rstrip() + "…"


def _bucket_label(bucket: str) -> str:
    return {
        "security": "Security-Updates",
        "regular": "reguläre Updates",
        "images": "Docker-Image-Updates",
    }.get(bucket or "", "Updates")


def _filter_label(package_filter: str) -> str:
    return {
        "security": "nur Security-Pakete",
        "all": "alle ausstehenden Pakete",
        "selected": "ausgewählte Pakete",
        "images": "Docker-Images",
        "release-upgrade": "Release-Upgrade",
    }.get(package_filter or "", package_filter or "Updates")


def explain_wave_item(item: dict[str, Any]) -> str:
    """3–5 German sentences: why this item, why waiting/failed, what’s next."""
    name = (item.get("target_name") or item.get("target_id") or "dieser Host").strip()
    bucket = str(item.get("bucket") or "")
    status = str(item.get("status") or "")
    reasons = [str(r) for r in (item.get("confirm_reasons") or []) if r]
    gates = [str(g) for g in (item.get("gates") or []) if g]
    error = _clean(str(item.get("error_message") or item.get("error") or ""))
    filt = _filter_label(str(item.get("package_filter") or ""))
    pkgs = item.get("packages") or []
    n = len(pkgs) if isinstance(pkgs, list) else 0

    sentences: list[str] = []
    sentences.append(
        f"Diese Welle spielt auf {name} {_bucket_label(bucket)} ein ({filt}"
        + (f", {n} Paket(e)" if n and bucket != "images" else "")
        + ")."
    )
    if "no-auto-patch" in reasons:
        sentences.append(
            "Der Gast trägt das Tag no-auto-patch — nichts wird automatisch eingespielt."
        )
    elif "kernel" in reasons:
        sentences.append(
            "Kernel-Pakete warten auf deine Bestätigung, weil ein Neustart wahrscheinlich ist."
        )
    elif "docker" in reasons:
        sentences.append(
            "Docker-Engine-Pakete warten auf Bestätigung, damit laufende Container nicht unerwartet stoppen."
        )
    elif bucket == "images":
        sentences.append(
            "Image-Updates kommen zuletzt und nur nach Bestätigung — kein neuer Image-Pfad, derselbe Patcher-Apply."
        )
    elif bucket == "security" and status in ("ready", "planned", "running"):
        sentences.append(
            "Security steht vorn in der Welle (security/ESM/unattended-security)."
        )
    elif bucket == "regular" or "regular" in reasons or "ambiguous" in reasons:
        sentences.append(
            "Nicht-Security oder unklare Pakete werden nicht automatisch eingespielt."
        )

    if gates:
        sentences.append("Gates blockieren Auto-Apply: " + " ".join(gates[:3]))
    if status in ("waiting_confirm", "blocked"):
        sentences.append(
            "Als Nächstes: prüfen, dann „Bestätigen“ — erst danach läuft der bestehende Apply mit Snapshot."
        )
    elif status == "running":
        sentences.append("Der Apply läuft gerade über die bestehende Patcher-Pipeline.")
    elif status == "success":
        if is_via_agent(item):
            sentences.append(agent_phrase("patches_applied" if bucket != "images" else "images_applied") + ".")
        else:
            sentences.append("Fertig. Der nächste Host der Welle folgt nur nach diesem Erfolg.")
    elif status == "failed":
        sentences.append(
            "Die Welle stoppt hier — kein stiller Retry."
            + (f" {error}" if error else "")
        )
        rb = item.get("rollback") if isinstance(item.get("rollback"), dict) else None
        if rb and rb.get("status") == "ok":
            sentences.append(
                agent_phrase("rolled_back", snap=str(rb.get("snap_name") or ""))
            )
        elif rb and rb.get("status") == "failed":
            sentences.append(
                by_agent("Rollback fehlgeschlagen — bitte den Host prüfen. Kein weiterer Versuch.")
            )
        elif rb and rb.get("status") == "skipped":
            sentences.append(by_agent(str(rb.get("error") or "Kein Pre-Apply-Snapshot — Rollback übersprungen.")))
        else:
            sentences.append(
                "Vor dem nächsten Versuch den Host prüfen; ein hlops-Snapshot (PATCHER_SNAP_KEEP) "
                "kann das Rollback erleichtern."
            )
    elif status == "skipped":
        sentences.append("Übersprungen, weil die Welle gestoppt oder nach einem Fehler beendet wurde.")
    else:
        sentences.append("Gestartet wird nur über „Welle starten“ oder eine Bestätigung — nie per DistUpgrade.")

    return " ".join(sentences[:5])


def explain_apply_run(run: dict[str, Any]) -> str:
    """History card explanation from a persisted apply_run row."""
    filt = str(run.get("package_filter") or "")
    status = str(run.get("status") or "")
    name = (run.get("target_name") or run.get("target_id") or "Host").strip()
    error = _clean(str(run.get("error_message") or ""))
    sentences: list[str] = []
    if filt == "release-upgrade":
        sentences.append(f"Release-Upgrade auf {name} — nie Teil einer automatischen Welle.")
    elif filt == "security":
        sentences.append(f"Security-Apply auf {name} über die bestehende Patcher-Pipeline.")
    elif filt == "images":
        sentences.append(f"Image-Apply auf {name} (bestehender Image-Pfad).")
    else:
        sentences.append(f"Apply ({_filter_label(filt)}) auf {name}.")
    if status == "failed":
        sentences.append("Fehlgeschlagen. Die Welle stoppt bei einem Apply-Fehler ohne Retry.")
        if error:
            sentences.append(error)
        rb = run.get("rollback") if isinstance(run.get("rollback"), dict) else None
        if rb and rb.get("status") == "ok":
            sentences.append(agent_phrase("rolled_back", snap=str(rb.get("snap_name") or "")))
        elif is_via_agent(run):
            sentences.append(by_agent("Logs und den hlops-Snapshot prüfen, bevor du neu planst."))
        else:
            sentences.append(
                "Logs und einen hlops-Snapshot (PATCHER_SNAP_KEEP) prüfen, bevor du neu planst."
            )
    elif status == "running":
        sentences.append("Läuft noch — Snapshot zuerst, dann apt/dnf/apk wie bisher.")
        if is_via_agent(run):
            sentences.append(by_agent("Der Agent spielt ein."))
    else:
        if is_via_agent(run):
            key = "images_applied" if filt == "images" else "patches_applied"
            sentences.append(agent_phrase(key) + ".")
        else:
            sentences.append("Eingespielt. Nächster Host der Welle nur nach Erfolg.")
        if run.get("reboot_required"):
            sentences.append("Reboot empfohlen — bitte manuell bestätigen, kein automatischer Neustart.")
    return " ".join(sentences[:5])


def explain_patch_job(job: dict[str, Any]) -> str:
    """Rule-based explanation for scan/apply job cards. Works without an API key."""
    kind = str(job.get("kind") or "")
    status = str(job.get("status") or "")
    target = (job.get("target_id") or "").strip()
    phase = (job.get("phase") or "").strip()
    error = _clean(str(job.get("error") or ""))
    message = _clean(str(job.get("message") or ""), max_len=220)
    wave = job.get("wave_item") if isinstance(job.get("wave_item"), dict) else None
    if wave:
        return explain_wave_item({**wave, "error_message": error or wave.get("error_message")})

    kind_de = {
        "scan": "Scan",
        "apply": "Apply",
        "apply-batch": "Stapel-Apply",
        "release-upgrade": "Release-Upgrade",
        "image-scan": "Image-Scan",
        "image-apply": "Image-Apply",
        "wave": "Wellen-Auftrag",
    }.get(kind, "Auftrag")

    sentences: list[str] = []
    if target:
        sentences.append(f"{kind_de} für {target}.")
    else:
        sentences.append(f"{kind_de} in der Patcher-Warteschlange.")

    if kind == "release-upgrade":
        sentences.append("Release-Upgrades startet der Wellen-Agent nie — nur nach expliziter Bestätigung.")
    elif kind in ("apply", "apply-batch", "image-apply"):
        sentences.append("Einspielen läuft über die bestehende Apply-Pipeline inklusive optionalem Proxmox-Snapshot.")
    elif kind in ("scan", "image-scan"):
        sentences.append("Nur Bestandsaufnahme — es wird nichts eingespielt.")

    if status in ("queued", "running"):
        sentences.append(
            "Status: "
            + ("wartet in der Warteschlange." if status == "queued" else f"läuft ({phase or 'Arbeit'}).")
        )
        if message:
            sentences.append(message)
    elif status == "success":
        if is_via_agent(job):
            if kind == "image-apply":
                sentences.append(agent_phrase("images_applied") + ".")
            elif kind in ("apply", "apply-batch"):
                sentences.append(agent_phrase("patches_applied") + ".")
            else:
                sentences.append(by_agent(message or "Erfolgreich abgeschlossen."))
        else:
            sentences.append(message or "Erfolgreich abgeschlossen.")
        sentences.append("Als Nächstes: nächsten Host der Welle oder manuell den Scan prüfen.")
    elif status == "failed":
        sentences.append("Fehlgeschlagen — die Welle stoppt bei einem Apply-Fehler ohne Retry-Schleife.")
        if error or message:
            sentences.append(error or message)
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        rb = result.get("rollback") if isinstance(result, dict) else None
        if not rb and isinstance(job.get("rollback"), dict):
            rb = job.get("rollback")
        if rb and rb.get("status") == "ok":
            sentences.append(agent_phrase("rolled_back", snap=str(rb.get("snap_name") or "")))
        elif rb and rb.get("status") == "failed":
            sentences.append(by_agent("Rollback fehlgeschlagen — kein weiterer Versuch."))
        elif rb and rb.get("status") == "skipped":
            sentences.append(by_agent(str(rb.get("error") or "Kein Pre-Apply-Snapshot.")))
        else:
            sentences.append(
                "Logs und einen hlops-Snapshot (PATCHER_SNAP_KEEP) prüfen, bevor du neu planst."
            )
    else:
        sentences.append(message or "Bereit.")

    return " ".join(sentences[:5])


async def maybe_enrich_explanation(
    rule_text: str,
    *,
    context: dict[str, Any],
    settings: PatcherSettings | None = None,
) -> str:
    """Optional LLM polish. Never required; never send secrets or env files."""
    settings = settings or get_patcher_settings()
    if not settings.llm_configured:
        return rule_text
    safe: dict[str, Any] = {}
    for key in (
        "target_name",
        "bucket",
        "status",
        "package_filter",
        "confirm_reasons",
        "gates",
        "kind",
        "phase",
    ):
        if key in context and context[key] not in (None, ""):
            safe[key] = context[key]
    pkgs = context.get("packages") or []
    if isinstance(pkgs, list):
        names: list[str] = []
        for p in pkgs[:20]:
            if isinstance(p, str):
                names.append(p)
            elif isinstance(p, dict) and p.get("name"):
                names.append(str(p["name"]))
        if names:
            safe["packages"] = names
    err = str(context.get("error_message") or context.get("error") or "")
    if err and not _SECRET_HINT.search(err):
        safe["error"] = _clean(err, max_len=200)

    user_prompt = (
        "Regeltext:\n"
        f"{rule_text}\n\n"
        f"Kontext (keine Secrets): {safe}\n\n"
        "Formuliere auf Deutsch 3–5 kurze Sätze für den Admin: warum dieser Auftrag, "
        "warum warten/fehlgeschlagen, was als Nächstes. Keine Befehle, keine Keys, "
        "kein Markdown. Behalte die Fakten des Regeltexts."
    )
    try:
        text = await _llm_enrich(user_prompt, settings)
    except LlmError as exc:
        logger.info("LLM-Erklärung übersprungen: %s", exc.message)
        return rule_text
    except Exception:
        logger.info("LLM-Erklärung übersprungen", exc_info=True)
        return rule_text
    extra = (text or "").strip()
    return extra[:2000] if extra else rule_text


async def _llm_enrich(user_prompt: str, settings: PatcherSettings) -> str:
    import httpx

    if not settings.llm_configured:
        raise LlmError("Kein PATCHER_LLM_API_KEY gesetzt.")
    url = settings.patcher_llm_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.patcher_llm_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.patcher_llm_model,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Du erklärst Homelab-Patch-Aufträge knapp auf Deutsch. "
                    "Keine Secrets, keine Shell-Befehle, kein DistUpgrade."
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=min(20.0, settings.patcher_llm_timeout)) as client:
            resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise LlmError(f"LLM-Anfrage fehlgeschlagen: {exc}") from exc
    if resp.status_code >= 400:
        raise LlmError(f"LLM HTTP {resp.status_code}")
    try:
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LlmError("Unerwartete LLM-Antwort.") from exc
    text = (text or "").strip()
    if not text:
        raise LlmError("Leere LLM-Antwort.")
    return text[:2000]
