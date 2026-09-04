# Smart Backup Integrity Verifier

Compose-**stack** backups (not per-container) with three copies, checksum verify, history, schedule, and restore.

## What it does

1. **Preflight inventory** via `docker inspect` — compose files, `.env`, named volumes, readable bind mounts, plus gap warnings.
2. **Archive on the LXC** (SSH, same key as Docker discovery) under `BACKUP_LXC_DIR`.
3. **Copy to Copilot** under `BACKUP_COPILOT_DIR` (default: `$DATA_DIR/backups`).
4. **Copy to Synology** (SFTP) when configured; otherwise mark hop `skipped`.
5. **Retention** after success: LXC max 2, Copilot max 5, Synology max 10 (oldest deleted).
6. **Verify** SHA256 after each hop; overall status in SQLite history.
7. If Synology fails but LXC+Copilot OK → run status **`partial`**.

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
- Verlauf: [`/modules/backup_verifier/history`](/modules/backup_verifier/history)
- Stack cards: **Backup** / **Verlauf**
- API prefix: `/api/modules/backup_verifier/`
  - `GET /status` — setup / Synology / crontab
  - `GET /preflight?parent_id=&project=`
  - `POST /run` — body `{parent_id, project, quiesce?}` → background job (`job_id`); poll `GET /jobs/{id}` for percent/phase; `?wait=true` sync
  - `GET /jobs/{id}` — Fortschritt (percent, phase, log_lines, destinations)
  - `GET /history`, `GET /history/{id}`
  - `POST /history/{id}/restore` — `{confirm: true, source: "copilot"|"synology"}`
  - `GET|POST /schedules`, `DELETE /schedules/{id}`, `POST /schedules/sync`

## Schedule (cron)

Schedules live in SQLite. The app syncs a **marker-managed** block into the user crontab when `crontab` exists:

```
# --- HOMELAB-COPILOT-BACKUP-VERIFIER BEGIN ---
0 3 * * * curl -fsS -X POST http://127.0.0.1:6655/api/modules/backup_verifier/run ...
# --- HOMELAB-COPILOT-BACKUP-VERIFIER END ---
```

Inside Docker there is usually **no cron daemon** — copy the preview block onto the **host** crontab and set `BACKUP_API_BASE` to a reachable Copilot URL.

## Config (env)

See `.env.example`:

- `BACKUP_COPILOT_DIR`, `BACKUP_LXC_DIR`
- `BACKUP_LXC_KEEP` / `BACKUP_COPILOT_KEEP` / `BACKUP_SYNOLOGY_KEEP`
- `BACKUP_SYNOLOGY_HOST`, `_USER`, `_PATH`, `_KEY_PATH` (optional → Docker SSH key)
- `BACKUP_QUIESCE`, `BACKUP_API_BASE`
- `BACKUP_SSH_TIMEOUT` (kurze SSH-Befehle, Default 120s)
- `BACKUP_ARCHIVE_TIMEOUT` / `BACKUP_TRANSFER_TIMEOUT` (tar/SCP, Default 3600s; Archiv läuft detached + Poll)

DB: `$DATA_DIR/backup_verifier.db`

## Honesty

Stack-level backup for typical Compose stacks. Bind mounts included when readable. **Not** a substitute for Proxmox `vzdump` for full LXC disaster recovery.
