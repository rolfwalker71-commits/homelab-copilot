# Smart Backup Integrity Verifier

Compose-**stack** backups (not per-container) with a configurable destination pipeline, checksum verify, history, schedule, and restore.

Default engine is **Voll (tar)** — full `.tar.gz` archives. **Incremental (restic)** is opt-in per run or schedule (Paperless-Größe: erster Lauf lang, danach nur Deltas).

## What it does

1. **Preflight inventory** via `docker inspect` — compose files, `.env`, named volumes, readable bind mounts, plus gap warnings.
2. **Ordered pipeline** (configured under **Ziele** in the UI):
   - `host_staging` — build archive on the LXC (ephemeral; purged after the first durable hop)
   - `copilot` — copy to the Copilot host path
   - `sftp` — Synology, Hetzner Storage Box, or custom SSH/SFTP (restic: rsync-over-SSH when possible)
3. **Retention** per durable destination (`keep_count` in the UI; env values only seed defaults).
4. **Verify** SHA256 after each hop; overall status in SQLite history.
5. If a later SFTP/rsync hop fails but Copilot succeeded → run status **`partial`**.

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
- **Ziele:** [`/modules/backup_verifier/destinations`](/modules/backup_verifier/destinations) — pipeline order, SFTP/SSH auth, connection check
- **Durchsuchen:** [`/modules/backup_verifier/browser`](/modules/backup_verifier/browser) — list Copilot + dest folders (`.tar.gz` and `restic/<host>/<stack>`), no binary dump; optional archive download
- Verlauf: [`/modules/backup_verifier/history`](/modules/backup_verifier/history)
- Stack cards: **Backup** / **Verlauf**
- API prefix: `/api/modules/backup_verifier/`
  - `GET /status` — setup / `pipeline` / in-process scheduler
  - `GET|PUT /destinations` — sorted list (secrets masked); full replace on PUT
  - `POST /destinations/check` — connection test for one destination payload
  - `GET /browse?dest_id=&path=` — list files/dirs under a dest root (path relative; `../` rejected)
  - `GET /browse/download?dest_id=&path=` — download a `.tar.gz` / `.tar` archive only (not restic keys/packs)
  - `GET /preflight?parent_id=&project=`
  - `POST /run` — body `{parent_id, project, quiesce?, engine?: tar|restic, restic_*?}` → background job (`job_id`); poll `GET /jobs/{id}` for percent/phase; `?wait=true` sync. **TOTP-gated** (not for host cron).
  - `GET /jobs?active=1` — laufende Jobs (Reconnect nach Navigation)
  - `GET /jobs/{id}` — Fortschritt (percent, phase, log_lines, destination hops, snapshot_id)
  - `GET /history`, `GET /history/{id}`
  - `POST /history/{id}/restore` — `{confirm: true, source: "<destination id|copilot|synology>", snapshot_id?}`
  - `GET /restic/snapshots?parent_id=&project=` — restic-Snapshots eines Stacks
  - `POST /restic/restore` — `{confirm, parent_id, project, snapshot_id, source}`
  - `GET|POST /schedules`, `PUT|PATCH /schedules/{id}`, `DELETE /schedules/{id}`, `POST /schedules/sync`

## Schedule (in-process)

Schedules live in SQLite and run **inside the Copilot process** (Europe/Berlin), same pattern as the patcher daily scan. No host crontab, no `crond` in the image, no curl.

Do **not** add crontab lines by hand. Older marker blocks that `curl` `/api/modules/backup_verifier/run` fail with **401** after TOTP (the run endpoint is not public). You can delete this block from the Docker host:

```
# --- HOMELAB-COPILOT-BACKUP-VERIFIER BEGIN ---
… curl … /api/modules/backup_verifier/run …
# --- HOMELAB-COPILOT-BACKUP-VERIFIER END ---
```

Leftover host curls log to `/tmp/homelab-backup-verifier-cron.log` on the machine that owns crontab (typically 401). In-app runs appear under **Verlauf**.

## Config

**Prefer the Ziele UI** for hosts, users, keys/passwords, keep counts, and order.

Optional env seed / paths (see `.env.example`):

- `BACKUP_COPILOT_DIR`, `BACKUP_LXC_DIR` — local paths (still used at runtime)
- `BACKUP_LXC_KEEP` / `BACKUP_COPILOT_KEEP` / `BACKUP_SYNOLOGY_KEEP` — seed keep counts
- `BACKUP_SYNOLOGY_*` — optional first-start seed for one SFTP destination
- `BACKUP_QUIESCE`
- `BACKUP_API_BASE` — unused for firing (legacy host-cron URL; schedules are in-process)
- `BACKUP_SSH_TIMEOUT` — short SSH (compose stop/start, `command -v restic`); default 120s. Not used for apt-get.
- `BACKUP_ARCHIVE_TIMEOUT` — wall-clock for tar / restic backup / extract (nohup+poll); default 3600s
- `BACKUP_TRANSFER_TIMEOUT` — SCP/SFTP/rsync hops and restic binary copy; default 3600s
- `RESTIC_INSTALL` — default `true`: if `restic` is missing, copy the Copilot image binary via SCP, else apt/apk via nohup+poll
- `RESTIC_INSTALL_TIMEOUT` — wall-clock for apt/apk bootstrap only; default 600s (never `BACKUP_SSH_TIMEOUT`)
- `BACKUP_RSYNC_INSTALL` — default `true`: if guest `rsync` is missing, apt/apk via nohup+poll, then rsync-over-SSH; SFTP only if that fails
- `BACKUP_RSYNC_INSTALL_TIMEOUT` — wall-clock for apt/apk rsync bootstrap; default 600s (same as restic)
- Hetzner Storage Box dest hop: Copilot already has `rsync` in the image. After the guest→Copilot mirror, Copilot→box uses `rsync -e "ssh -p 23"` when possible. In **Hetzner Robot** enable **SSH-Unterstützung**. Port **23** = SSH/rsync/Borg; port **22** = SFTP only (fallback if SSH-23 is off, the key is rejected, or local rsync is missing). An explicit dest port wins; `*.your-storagebox.de` / preset `storage_box` with unset or SFTP-22 defaults rsync to 23. The box already speaks rsync — Copilot does not install packages on the dest.

DB: `$DATA_DIR/backup_verifier.db` (restic repo password lives here, never in git)

## Incremental (restic)

Opt-in on **Backup** / **Zeitplan**: Engine *Incremental (restic)*.

1. First run per host: `command -v restic`. If missing and `RESTIC_INSTALL=true`, copy `/usr/bin/restic` from the Copilot container (same Debian/amd64 binary as the image). If that fails, apt/apk runs detached (nohup+poll, `RESTIC_INSTALL_TIMEOUT`).
1b. LXC→Copilot repo sync: `command -v rsync` on the guest. If missing and `BACKUP_RSYNC_INSTALL=true`, apt/apk installs rsync (nohup+poll, `BACKUP_RSYNC_INSTALL_TIMEOUT`). Copilot already ships `rsync` in the image. SFTP is last resort if install fails or hangs.
1c. Copilot→Hetzner (or other SFTP dest): prefer rsync-over-SSH (Storage Box: port 23 unless the dest sets another port). If rsync/SSH-23 fails, SFTP on port 22 with a German job-log reason (Robot SSH-Unterstützung, auth, or local rsync missing). `--info=progress2` when the box supports it; older Storage Box rsync retries without it.
2. Repo password is generated once per stack and stored in SQLite (`restic_secrets`).
3. Working repo on the LXC: `$BACKUP_LXC_DIR/restic/{project}`. Durable copy: `$BACKUP_COPILOT_DIR/restic/{parent}/{project}`. Dest hops get the same repo tree (`…/restic/{parent}/{project}`).
4. Same inventory as tar (compose, named volume mountpoints, readable binds). Quiesce still stops the stack.
5. Every N days (default 7): `restic forget --keep-last/--keep-weekly --prune` and `restic check`. Restic has no classic full backup — a snapshot is always a complete restore point.
6. Restore: Verlauf → Snapshots listen → Snapshot wählen → gleiche Ziele wie tar.

**Paperless:** choose the paperless stack, Engine Incremental, Preflight, confirm. First run copies all media (can take hours; raise `BACKUP_ARCHIVE_TIMEOUT` if needed). Later runs send only new/changed chunks.

Do **not** run restic by hand or add crontab. Passwords are not shown in the UI or logs.

## Honesty

Stack-level backup for typical Compose stacks. Bind mounts included when readable. **Not** a substitute for Proxmox `vzdump` for full LXC disaster recovery.
