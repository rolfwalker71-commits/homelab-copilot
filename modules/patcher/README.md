# AI-Driven Patch-Management

Scannt und spielt Linux-Updates auf **Proxmox-Guests (LXC/QEMU)** und **manuell hinterlegten Hosts** ein.

## Features

- Paketmanager: `apt`, `dnf`/`yum`, `apk` (Auto-Detect)
- Heuristik Security vs. Normal
- Optional: LLM-Zusammenfassung (OpenAI-kompatibel / Ollama)
- Apply nur mit `confirm=true` (Security / Alle / Auswahl)
- Release-Upgrade (Ubuntu): Scan schlägt das **nächste unterstützte** Release bzw. **nächste LTS** vor — nie ein schon EOL-Zwischenrelease. Ausführen nur mit `confirm=true` (ein Confirm für den ganzen Pfad; unter der Haube sequenzielle `do-release-upgrade`-Schritte).
- Reboot nur melden + optionaler Reboot mit Bestätigung
- Scan-Zeitplan via Crontab (kein Auto-Apply, kein Auto-Release-Upgrade)

## Release-Upgrade (Ubuntu)

- **24.10 / EOL-Interims** (24.10, 25.04, 25.10 Stand 2026-09): Empfehlung **→ 26.04 LTS**. Ausführung: `24.10 → 25.04 → 25.10 → 26.04` (Ubuntu kann nicht überspringen). Ein Confirm.
- **24.04 LTS**: leise „Nächstes LTS 26.04 verfügbar“ (kein EOL-Text). Ein `do-release-upgrade` mit `Prompt=lts`.
- **Debian**: nur Hinweis bookworm→trixie, kein automatisches Einspielen.
- LXC: Container-Erkennung, NEEDRESTART aus, Snap-Refresh zurückhalten. Kein Proxmox-Host-Upgrade.

## UI

- `/modules/patcher` — Ziele, Scan, Apply, Docker-Images (nur nach Bestätigung)
- `/modules/patcher/hosts` — manuelle Hosts (erscheinen unter Topologie → Linux)
- `/modules/patcher/schedule` — Scan-Cron
- `/modules/patcher/history` — Verlauf

## API (Auszug)

| Methode | Pfad | Zweck |
|---------|------|--------|
| GET | `/api/modules/patcher/status` | Status |
| GET | `/api/modules/patcher/targets` | Guests + Manual |
| POST | `/api/modules/patcher/scan` | Scan-Job |
| GET | `/api/modules/patcher/jobs?active=1` | Laufende Scan/Apply-Jobs (Reconnect) |
| GET | `/api/modules/patcher/jobs/{id}` | Job-Fortschritt |
| POST | `/api/modules/patcher/apply` | Apply-Job (`confirm`) |
| POST | `/api/modules/patcher/release-upgrade` | Release-Upgrade (`confirm`, nie automatisch) |
| POST | `/api/modules/patcher/summarize` | LLM nachträglich |
| CRUD | `/api/modules/patcher/hosts` | Manuelle Hosts |

## Env

```bash
PATCHER_LLM_API_KEY=
PATCHER_LLM_BASE_URL=https://api.openai.com/v1
PATCHER_LLM_MODEL=gpt-4o-mini   # z.B. gpt-4.1-mini — nicht gpt-41-mini
# PATCHER_API_BASE=http://127.0.0.1:6655   # für Cron-curl
# PATCHER_RELEASE_TIMEOUT=10800           # do-release-upgrade je Schritt
```

Unter Docker müssen `PATCHER_LLM_*` in `docker-compose.yml` → `environment:` stehen (sonst bleibt der Key nur in der Host-`.env` und die UI meldet „Kein LLM-Key“). Nach Änderung: `docker compose up -d` (Container neu erzeugen).
SSH nutzt denselben Key/User wie Docker-Discovery (`DOCKER_SSH_*`).
