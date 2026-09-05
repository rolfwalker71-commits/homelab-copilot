# Homelab Operations Copilot

Phase-1-Foundation: Zero-Config Discovery für **Proxmox** (Nodes, LXC, QEMU) und **Docker** (lokaler Socket + SSH), unified Topology-Cache, erweiterbares Modul-Framework — als **PWA** (installierbar, mobil-tauglich).

Spätere / vorhandene Module (ohne Core-Rewrite): AI-Driven Patch-Management (`modules/patcher/`), Smart Backup Integrity Verifier (`modules/backup_verifier/`).

## Stack

- Backend: Python 3.12 · FastAPI (async)
- Frontend: Tailwind (CDN) · Jinja2 · Progressive Web App
- Persistenz: SQLite unter `/data`
- Port: **6655**
- Locale: Deutsch (`DD.MM.YYYY, HH:mm:ss Uhr`, Zone `Europe/Berlin`)
- Image: `linux/amd64` → GHCR

## Schnellstart (Docker)

Remote/Produktion läuft **nur** über GHCR (kein `--build` auf dem Server):

```bash
cp .env.example .env   # Proxmox-Credentials eintragen
docker compose pull
docker compose up -d
```

App: http://localhost:6655  

Lokal bauen:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml build
docker compose up -d
```

### Entwicklung ohne Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATA_DIR=./data MODULES_DIR=./modules TZ=Europe/Berlin
python -m uvicorn app.main:app --host 0.0.0.0 --port 6655 --reload
```

## Konfiguration

| Variable | Beschreibung |
|----------|--------------|
| `PROXMOX_HOST` | Proxmox-Host/IP |
| `PROXMOX_TOKEN_ID` / `PROXMOX_TOKEN_SECRET` | API-Token (bevorzugt) |
| `PROXMOX_PASSWORD` | Fallback-Login |
| `PROXMOX_VERIFY_SSL` | `true`/`false` |
| `DOCKER_USE_LOCAL_SOCKET` | `/var/run/docker.sock` scannen |
| `DOCKER_SSH_KEY_HOST_PATH` | Host-Datei für den SSH-Key (Compose → `/data/ssh/id_ed25519:ro`) |
| `DOCKER_SSH_KEY_PATH` | Key-Pfad *im Container* (Default `/data/ssh/id_ed25519`) |
| `DISCOVERY_INTERVAL_SECONDS` | Auto-Refresh (Default 300) |

### Volumes / SSH-Key (Produktion)

- Persistenz: Named Volume `copilot-data` → `/data` (SQLite `topology.db`, Backups, …).
- SSH-Key: **nur** die Key-Datei bind-mounten, z. B. `DOCKER_SSH_KEY_HOST_PATH=/home/homelab-copilot/data/ssh/id_ed25519` → `/data/ssh/id_ed25519:ro`.
- **Nicht** den Key oder ein Host-Verzeichnis auf `/data` mounten — sonst ersetzt der Mount das Data-Volume und die App crash-loopt mit `unable to open database file` (Prozess läuft als UID `10001`).
- Key-Datei auf dem Host **vor** `compose up` anlegen (`chmod 600`); fehlt die Datei, legt Docker dort ein Verzeichnis an.

### Proxmox API-Token ACL (wichtig)

Tokens mit **Privilege Separation** erben **nicht** die Rechte von `root@pam`. Ohne ACL liefert Proxmox oft **HTTP 200 mit leerer** `/lxc`-/`/qemu`-Liste (wirkt wie „0 Guests“).

In der Proxmox-UI: **Datacenter → Permissions → Add**

| Feld | Wert |
|------|------|
| Path | `/` |
| API Token | `root@pam!copilot` (bzw. dein Token) |
| Role | `PVEAuditor` (enthält `VM.Audit` + `Sys.Audit`) |
| Propagate | ja |

Alternativ Privilege Separation am Token deaktivieren (Token erbt dann User-Rechte) — weniger restriktiv.

Alternativ: Setup-Assistent unter `/setup` (Laufzeit; für Persistenz Env-Vars setzen).

## PWA / Mobile

- Web App Manifest + Service Worker (`/sw.js`)
- **Mobile (&lt; lg):** Material You 3 Expressive — Flush-NavigationBar, tonal Surfaces, Squircle-FAB, Seed `#6750A4`
- **Desktop (lg+):** Fluent 2 — Mica-Header, kompakter Radius, Accent-Underline-Nav (`#60cdff` / `#005fb8`)
- Chrome-Auto: `document.documentElement.dataset.chrome` (`android` | `desktop`); Override via `localStorage.hlops-chrome`
- Darstellung: `data-theme` (`light` | `dark`); Wahl via `localStorage.hlops-theme` (`system` | `light` | `dark`)
- Installierbar (Chrome/Edge/Android; iOS: Teilen → „Zum Home-Bildschirm“)
- Offline-Fallback unter `/offline`

Hinweis: Service Worker verlangen **HTTPS** (oder `localhost`).

## Modul-Framework

Drop-in unter `modules/<name>/module.py` mit Export `MODULE` (siehe `modules/example/`):

```python
class MyModule:
    name = "patcher"
    version = "0.1.0"
    description = "…"
    def get_router(self): ...
    async def on_startup(self, app): ...
    async def on_topology_refresh(self, topology: dict): ...
```

Router landen unter `/api/modules/<name>/…`.

## API (Auszug)

| Methode | Pfad | Zweck |
|---------|------|--------|
| GET | `/api/health` | Health |
| GET | `/api/topology` | Aktuelle Topologie |
| POST | `/api/discovery/refresh` | Manuelle Discovery |
| GET | `/api/modules` | Geladene Module |
| GET/POST | `/api/setup` | Setup-Status / Laufzeit-Config |

OpenAPI: `/api/docs`

## CI/CD

Workflow [`.github/workflows/build-ghcr.yml`](.github/workflows/build-ghcr.yml) baut und pusht `linux/amd64` nach:

`ghcr.io/rolfwalker71-commits/homelab-copilot`

## Projektstruktur

```
app/
  main.py              # FastAPI + PWA-Routen
  config.py
  core/                # discovery, registry, topology, locale
  api/
  static/              # CSS, SW, manifest, icons
  templates/           # Dashboard, Setup, Offline
modules/
  example/             # Demo-Plugin
  patcher/             # AI-Driven Patch-Management (Scan/Apply)
  backup_verifier/     # Smart Backup Integrity Verifier
Dockerfile
docker-compose.yml           # GHCR image only
docker-compose.build.yml     # lokales build overlay
```

## Lizenz / Status

Internes Homelab-Projekt · Phase 1 Foundation.
