"""In-memory patch job registry for UI progress polling."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ops_agent.actor import actor_fields


@dataclass
class PatchJob:
    id: str
    kind: str  # scan | apply | apply-batch | release-upgrade | image-scan | image-apply
    target_id: str
    via_agent: bool = False
    status: str = "queued"  # queued | running | success | failed
    phase: str = "Warteschlange"
    percent: int = 0
    message: str = ""
    log_lines: list[str] = field(default_factory=list)
    scan_id: int | None = None
    apply_id: int | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_output_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        last_out = self.last_output_at or self.updated_at
        alive = self.status in ("queued", "running")
        return {
            "job_id": self.id,
            "id": self.id,
            "kind": self.kind,
            "target_id": self.target_id,
            **actor_fields(via_agent=self.via_agent),
            "status": self.status,
            "phase": self.phase,
            "percent": max(0, min(100, int(self.percent))),
            "message": self.message,
            "log_lines": self.log_lines[-40:],
            "scan_id": self.scan_id,
            "apply_id": self.apply_id,
            "error": self.error,
            "result": self.result,
            "done": self.status in ("success", "failed"),
            "ok": self.status == "success",
            "updated_at": self.updated_at,
            "last_output_at": last_out,
            "alive": alive,
            "silence_seconds": max(0, int(now - last_out)),
        }


class JobRegistry:
    def __init__(self, *, max_jobs: int = 200) -> None:
        self._jobs: dict[str, PatchJob] = {}
        self._lock = threading.Lock()
        self._max_jobs = max_jobs

    def create(self, *, kind: str, target_id: str, via_agent: bool = False) -> PatchJob:
        job = PatchJob(
            id=str(uuid.uuid4()),
            kind=kind,
            target_id=target_id,
            via_agent=bool(via_agent),
            status="queued",
            phase="Warteschlange",
            percent=0,
            message="Auftrag in Warteschlange…",
        )
        with self._lock:
            self._jobs[job.id] = job
            self._prune_locked()
        return job

    def get(self, job_id: str) -> PatchJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_active(self) -> list[PatchJob]:
        with self._lock:
            jobs = [
                j
                for j in self._jobs.values()
                if j.status in ("queued", "running")
            ]
        jobs.sort(key=lambda j: j.updated_at, reverse=True)
        return jobs

    def set_progress(
        self,
        job_id: str,
        *,
        phase: str | None = None,
        percent: int | None = None,
        message: str | None = None,
        scan_id: int | None = None,
        apply_id: int | None = None,
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
            if scan_id is not None:
                job.scan_id = scan_id
            if apply_id is not None:
                job.apply_id = apply_id
            if job.status == "queued":
                job.status = "running"
            job.updated_at = time.time()

    def append_log(self, job_id: str, line: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            text = line.strip()
            if text and "libstdbuf.so" in text and "LD_PRELOAD" in text:
                return
            status = text.startswith("SSH-Sitzung offen")
            if text:
                job.log_lines.append(text)
                if len(job.log_lines) > 200:
                    job.log_lines = job.log_lines[-200:]
                if not status:
                    job.last_output_at = time.time()
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
            job.error = error
            job.phase = phase or ("Fertig" if status == "success" else "Fehler")
            if message:
                job.message = message
            elif status == "success":
                job.message = "Erfolgreich abgeschlossen."
            elif error:
                job.message = error
            else:
                job.message = "Fehlgeschlagen."
            if result:
                if result.get("scan_id") is not None:
                    try:
                        job.scan_id = int(result["scan_id"])
                    except (TypeError, ValueError):
                        pass
                if result.get("apply_id") is not None:
                    try:
                        job.apply_id = int(result["apply_id"])
                    except (TypeError, ValueError):
                        pass
            job.updated_at = time.time()

    def _prune_locked(self) -> None:
        if len(self._jobs) <= self._max_jobs:
            return
        finished = sorted(
            (
                j
                for j in self._jobs.values()
                if j.status in ("success", "failed")
            ),
            key=lambda j: j.updated_at,
        )
        overflow = len(self._jobs) - self._max_jobs
        for job in finished[:overflow]:
            self._jobs.pop(job.id, None)


JOBS = JobRegistry()
