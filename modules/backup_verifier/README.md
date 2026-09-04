# Smart Backup Integrity Verifier

Compose-**stack** backups (not per-container) with a configurable destination pipeline, checksum verify, history, schedule, and restore.

## What it does

1. **Preflight inventory** via `docker inspect` — compose files, `.env`, named volumes, readable bind mounts, plus gap warnings.
2. **Ordered pipeline** (configured under **Ziele** in the UI):
   - `host_staging` — build archive on the LXC (ephemeral; purged after the first durable hop)
   - `copilot` — copy to the Copilot host path
   - `sftp` — Synology, Hetzner Storage Box, or custom SSH/SFTP
3. **Retention** per durable destination (`keep_count` in the UI; env values only seed defaults).
4. **Verify** SHA256 after each hop; overall status in SQLite history.
5. If a later SFTP hop fails but Copilot succeeded → run status **`partial`**.

Credentials and host/paths live in **SQLite** (`backup_verifier.db`). Env vars are an optional **seed** on first start.

Unit of backup = **entire Compose project**. Containers share volumes/networks; restoring one container alone is wrong.

## Included / excluded

| Included | Excluded / gaps |
|----------|-----------------|
| Compose YAML from working_dir / labels | Anonymous volumes (container writable layer only) |
| `.env` next to compose (if readable) | Bind mounts not readable on LXC |
| `docker compose config` dump | External NFS/CIFS if unreadable from LXC |
| Named volumes (tar via helper container) | Data outside inspect mounts |
| Readable bind-mount host paths | Swarm / Kubernetes |
| Manifest JSON (DE + ISO time, mounts, checksums) | Full LXC disk (use Proxmox `vzdump`) |

Default **quiesce**: `docker compose stop` before volume tar, then `up -d` / `start`. Override with `BACKUP_QUIESCE=false` or API `quiesce`.

## UI & API

- Page: [`/modules/backup_verifier`](/modules/backup_verifier) (Backup / Zeitplan, volle Breite)
- **Ziele:** [`/modules/backup_verifier/destinations`](/modules/backup_verifier/destinations) — pipeline order, SFTP auth, connection check
- Verlauf: [`/modules/backup_verifier/history`](/modules/backup_verifier/history)
- Stack cards: **Backup** / **Verlauf**
- API prefix: `/api/modules/backup_verifier/`
  - `GET /status` — setup / `pipeline` / crontab
  - `GET|PUT /destinations` — sorted list (secrets masked); full replace on PUT
  - `POST /destinations/check` — connection test for one destination payload
  - `GET /preflight?parent_id=&project=`
  - `POST /run` — body `{parent_id, project, quiesce?}` → background job (`job_id`); poll `GET /jobs/{id}` for percent/phase; `?wait=true` sync
  - `GET /jobs/{id}` — Fortschritt (percent, phase, log_lines, destination hops)
  - `GET /history`, `GET /history/{id}`
  - `POST /history/{id}/restore` — `{confirm: true, source: "<destination id|copilot|synology>"}`
  - `GET|POST /schedules`, `DELETE /schedules/{id}`, `POST /schedules/sync`

## Schedule (cron)

Schedules live in SQLite. The app syncs a **marker-managed** block into the user crontab when `crontab` exists:

```
# --- HOMELAB-COPILOT-BACKUP-VERIFIER BEGIN ---
0 3 * * * curl -fsS -X POST http://127.0.0.1:6655/api/modules/backup_verifier/run ...
# --- HOMELAB-COPILOT-BACKUP-VERIFIER END ---
```

Inside Docker there is usually **no cron daemon** — copy the preview block onto the **host** crontab and set `BACKUP_API_BASE` to a reachable Copilot URL.

## Config

**Prefer the Ziele UI** for hosts, users, keys/passwords, keep counts, and order.

Optional env seed / paths (see `.env.example`):

- `BACKUP_COPILOT_DIR`, `BACKUP_LXC_DIR` — local paths (still used at runtime)
- `BACKUP_LXC_KEEP` / `BACKUP_COPILOT_KEEP` / `BACKUP_SYNOLOGY_KEEP` — seed keep counts
- `BACKUP_SYNOLOGY_*` — optional first-start seed for one SFTP destination
- `BACKUP_QUIESCE`, `BACKUP_API_BASE`
- `BACKUP_SSH_TIMEOUT` / `BACKUP_ARCHIVE_TIMEOUT` / `BACKUP_TRANSFER_TIMEOUT`

DB: `$DATA_DIR/backup_verifier.db`

## Honesty

Stack-level backup for typical Compose stacks. Bind mounts included when readable. **Not** a substitute for Proxmox `vzdump` for full LXC disaster recovery.
