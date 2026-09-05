"""In-memory backup job registry for UI progress polling."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BackupJob:
    id: str
    parent_id: str
    project: str
    kind: str = "backup"  # backup | restore | wipe
    status: str = "queued"  # queued | running | success | partial | failed
    phase: str = "Warteschlange"
    percent: int = 0
    message: str = "Backup wird vorbereitet…"
    log_lines: list[str] = field(default_factory=list)
    run_id: int | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        dest: dict[str, str] = {}
        dest_list: list[dict[str, Any]] = []
        run = self.result or {}
        hops = run.get("destinations")
        if isinstance(hops, list) and hops:
            dest_list = [
                {
                    "id": h.get("id"),
                    "kind": h.get("kind"),
                    "label": h.get("label") or h.get("kind"),
                    "status": h.get("status") or "—",
                    "verify": h.get("verify") or "—",
                }
                for h in hops
            ]
            for h in dest_list:
                key = str(h.get("kind") or h.get("label") or "hop")
                dest[key] = str(h.get("status") or "—")
        elif run:
            dest = {
                "lxc": str(run.get("lxc_status") or "—"),
                "copilot": str(run.get("copilot_status") or "—"),
                "synology": str(run.get("synology_status") or "—"),
            }
            dest_list = [
                {"kind": k, "label": k, "status": v, "verify": "—"}
                for k, v in dest.items()
            ]
        hist = "/modules/backup_verifier/history"
        return {
            "job_id": self.id,
            "id": self.id,
            "parent_id": self.parent_id,
            "project": self.project,
            "status": self.status,
            "phase": self.phase,
            "percent": max(0, min(100, int(self.percent))),
            "message": self.message,
            "log_lines": self.log_lines[-40:],
            "run_id": self.run_id,
            "error": self.error,
            "destinations": dest,
            "destination_hops": dest_list,
            "history_url": hist,
            "history_run_url": f"{hist}?run={self.run_id}" if self.run_id else hist,
            "done": self.status in ("success", "partial", "failed"),
            "ok": self.status in ("success", "partial"),
            "kind": self.kind,
            "engine": str(run.get("engine") or ""),
            "snapshot_id": run.get("snapshot_id") or "",
            "bytes_added": run.get("bytes_added"),
            "bytes_processed": run.get("bytes_processed"),
            "updated_at": self.updated_at,
        }


class JobRegistry:
    """Thread-safe in-process job store (cleared on process restart)."""

    def __init__(self, *, max_jobs: int = 200) -> None:
        self._jobs: dict[str, BackupJob] = {}
        self._lock = threading.Lock()
        self._max_jobs = max_jobs

    def create(self, *, parent_id: str, project: str, kind: str = "backup") -> BackupJob:
        if kind == "restore":
            kind_n = "restore"
            msg = "Restore läuft…"
        elif kind == "wipe":
            kind_n = "wipe"
            msg = "Zurücksetzen läuft…"
        else:
            kind_n = "backup"
            msg = "Backup läuft…"
        job = BackupJob(
            id=str(uuid.uuid4()),
            parent_id=parent_id,
            project=project,
            kind=kind_n,
            status="queued",
            phase="Warteschlange",
            percent=0,
            message=msg,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._prune_locked()
        return job

    def get(self, job_id: str) -> BackupJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_active(self) -> list[BackupJob]:
        with self._lock:
            jobs = [
                j
                for j in self._jobs.values()
                if j.status in ("queued", "running")
            ]
        jobs.sort(key=lambda j: j.updated_at, reverse=True)
        return jobs

    def set_running(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = "running"
            job.phase = "Preflight"
            job.percent = 2
            job.message = "Backup läuft…"
            job.updated_at = time.time()

    def set_progress(
        self,
        job_id: str,
        *,
        phase: str | None = None,
        percent: int | None = None,
        message: str | None = None,
        run_id: int | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if phase is not None:
                job.phase = phase
            if percent is not None:
                job.percent = max(0, min(100, int(percent)))
            if message is not None:
                job.message = message
            if run_id is not None:
                job.run_id = run_id
            if job.status == "queued":
                job.status = "running"
            job.updated_at = time.time()

    def append_log(self, job_id: str, line: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            text = line.strip()
            if text:
                job.log_lines.append(text)
                if len(job.log_lines) > 200:
                    job.log_lines = job.log_lines[-200:]
            job.updated_at = time.time()

    def finish(
        self,
        job_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        message: str | None = None,
        phase: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = status
            job.percent = 100
            job.result = result
            if result and result.get("id") is not None:
                try:
                    job.run_id = int(result["id"])
                except (TypeError, ValueError):
                    pass
            job.error = error
            job.phase = phase or (
                "Fertig"
                if status in ("success", "partial")
                else "Fehler"
            )
            noun = {"restore": "Restore", "wipe": "Zurücksetzen"}.get(
                job.kind, "Backup"
            )
            if message:
                job.message = message
            elif status == "success":
                job.message = f"{noun} erfolgreich abgeschlossen."
            elif status == "partial":
                job.message = f"{noun} teilweise erfolgreich (Ziele prüfen)."
            elif error:
                job.message = error
            else:
                job.message = f"{noun} fehlgeschlagen."
            job.updated_at = time.time()

    def _prune_locked(self) -> None:
        if len(self._jobs) <= self._max_jobs:
            return
        # Drop oldest finished jobs first
        finished = sorted(
            (
                j
                for j in self._jobs.values()
                if j.status in ("success", "partial", "failed")
            ),
            key=lambda j: j.updated_at,
        )
        overflow = len(self._jobs) - self._max_jobs
        for job in finished[:overflow]:
            self._jobs.pop(job.id, None)


JOBS = JobRegistry()
