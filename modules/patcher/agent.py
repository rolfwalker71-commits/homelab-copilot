"""Patch-Wellen-Agent: group pending work, gate auto-security, stop on fail.

Policy layer on top of existing patcher apply/scan jobs — never DistUpgrade,
never a second apply pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from patcher.config import get_patcher_settings
from patcher.explain import explain_wave_item, maybe_enrich_explanation
from patcher.jobs import JOBS
from patcher.store import PatcherStore

logger = logging.getLogger(__name__)

BUCKET_SECURITY = "security"
BUCKET_REGULAR = "regular"
BUCKET_IMAGES = "images"
BUCKET_RANK = {BUCKET_SECURITY: 0, BUCKET_REGULAR: 1, BUCKET_IMAGES: 2}

STATUS_PLANNED = "planned"
STATUS_READY = "ready"
STATUS_WAITING = "waiting_confirm"
STATUS_BLOCKED = "blocked"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

WAVE_PLANNED = "planned"
WAVE_RUNNING = "running"
WAVE_WAITING = "waiting"
WAVE_STOPPED = "stopped"
WAVE_FAILED = "failed"
WAVE_COMPLETED = "completed"

NO_AUTO_TAG = re.compile(r"^no[-_]?auto[-_]?patch$", re.I)
KERNEL_PKG = re.compile(
    r"^(linux-image|linux-headers|linux-modules|linux-generic|linux-system)([:-]|$)",
    re.I,
)
DOCKER_PKG = re.compile(
    r"^(docker-ce|docker-ce-cli|docker\.io|docker-compose|docker-compose-plugin|"
    r"containerd\.io|containerd)([:-]|$)",
    re.I,
)
SECURITY_BLOB = re.compile(
    r"(security|esm|unattended-security|ubuntu-security|debian-security)",
    re.I,
)

DEFAULT_DISK_CRITICAL_PCT = 90.0

ApplyJobFn = Callable[..., Awaitable[None]]
ImageApplyFn = Callable[..., Awaitable[None]]


@dataclass
class HostPending:
    target_id: str
    target_name: str
    packages: list[dict[str, Any]] = field(default_factory=list)
    image_updates: int = 0
    image_names: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class HostContext:
    target_id: str
    online: bool = True
    disk_pct: float | None = None
    backup_running: bool = False


@dataclass
class PlannedItem:
    target_id: str
    target_name: str
    bucket: str
    needs_confirm: bool
    confirm_reasons: list[str]
    package_filter: str
    packages: list[str]
    reason: str
    sort_order: int = 0

    def to_row(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "target_name": self.target_name,
            "bucket": self.bucket,
            "needs_confirm": self.needs_confirm,
            "confirm_reasons": self.confirm_reasons,
            "package_filter": self.package_filter,
            "packages": self.packages,
            "reason": self.reason,
            "sort_order": self.sort_order,
        }


@dataclass
class AgentPolicy:
    enabled: bool = False
    auto_security: bool = False
    max_parallel: int = 1


def has_no_auto_patch(tags: list[str] | None) -> bool:
    for raw in tags or []:
        if NO_AUTO_TAG.match(str(raw).strip()):
            return True
    return False


def _pkg_blob(pkg: dict[str, Any]) -> str:
    meta = pkg.get("meta") if isinstance(pkg.get("meta"), dict) else {}
    parts = [
        pkg.get("name"),
        pkg.get("priority"),
        pkg.get("archive"),
        pkg.get("repo"),
        meta.get("archive"),
        meta.get("repo"),
        meta.get("origin"),
        meta.get("section"),
    ]
    return " ".join(str(x) for x in parts if x)


def is_security_class(pkg: dict[str, Any]) -> bool:
    pri = str(pkg.get("priority") or "").lower()
    if pri == "security":
        return True
    return bool(SECURITY_BLOB.search(_pkg_blob(pkg)))


def package_confirm_reason(pkg: dict[str, Any]) -> str | None:
    """Return why this package must wait for confirm, or None if auto-security-eligible."""
    name = str(pkg.get("name") or "").strip()
    if KERNEL_PKG.match(name):
        return "kernel"
    if DOCKER_PKG.match(name):
        return "docker"
    if is_security_class(pkg):
        return None
    if not name:
        return "ambiguous"
    if not (pkg.get("priority") or pkg.get("archive") or pkg.get("repo") or pkg.get("meta")):
        return "ambiguous"
    return "regular"


def group_host_work(host: HostPending) -> list[PlannedItem]:
    """Split one host into security / confirm / images. Never includes DistUpgrade."""
    no_auto = has_no_auto_patch(host.tags)
    security: list[str] = []
    confirm: list[str] = []
    reasons: list[str] = []
    if no_auto:
        reasons.append("no-auto-patch")

    for pkg in host.packages or []:
        if not isinstance(pkg, dict):
            continue
        name = str(pkg.get("name") or "").strip()
        if not name:
            continue
        why = package_confirm_reason(pkg)
        if no_auto or why:
            if name not in confirm:
                confirm.append(name)
            if why and why not in reasons:
                reasons.append(why)
        elif name not in security:
            security.append(name)

    items: list[PlannedItem] = []
    if security:
        items.append(
            PlannedItem(
                target_id=host.target_id,
                target_name=host.target_name,
                bucket=BUCKET_SECURITY,
                needs_confirm=False,
                confirm_reasons=[],
                package_filter="security",
                packages=security,
                reason="Security zuerst (security/ESM/unattended-security).",
            )
        )
    if confirm:
        items.append(
            PlannedItem(
                target_id=host.target_id,
                target_name=host.target_name,
                bucket=BUCKET_REGULAR,
                needs_confirm=True,
                confirm_reasons=reasons,
                package_filter="selected",
                packages=confirm,
                reason="Wartet auf Bestätigung (Kernel, Docker, no-auto-patch oder Nicht-Security).",
            )
        )
    if int(host.image_updates or 0) > 0 or host.image_names:
        names = [n for n in (host.image_names or []) if n]
        items.append(
            PlannedItem(
                target_id=host.target_id,
                target_name=host.target_name,
                bucket=BUCKET_IMAGES,
                needs_confirm=True,
                confirm_reasons=["images"],
                package_filter="images",
                packages=names,
                reason="Images zuletzt — bestehender Image-Apply, nur nach Bestätigung.",
            )
        )
    return items


def group_wave(hosts: list[HostPending]) -> list[PlannedItem]:
    """Security first, then regular/confirm, images last. Stable per host name."""
    collected: list[PlannedItem] = []
    for host in hosts:
        collected.extend(group_host_work(host))
    collected.sort(
        key=lambda it: (
            BUCKET_RANK.get(it.bucket, 9),
            it.target_name.lower(),
            it.target_id,
        )
    )
    for idx, item in enumerate(collected):
        item.sort_order = idx
    return collected


def evaluate_gates(
    ctx: HostContext,
    *,
    disk_critical_pct: float = DEFAULT_DISK_CRITICAL_PCT,
) -> list[str]:
    """Reasons that block auto-apply. Empty = all gates pass."""
    blocked: list[str] = []
    if not ctx.online:
        blocked.append("Host ist nicht online.")
    if ctx.disk_pct is not None:
        try:
            if float(ctx.disk_pct) >= float(disk_critical_pct):
                blocked.append(
                    f"Disk ist kritisch ({float(ctx.disk_pct):.0f} % ≥ {float(disk_critical_pct):.0f} %)."
                )
        except (TypeError, ValueError):
            pass
    if ctx.backup_running:
        blocked.append("Backup oder Restore läuft auf diesem Gast/Host.")
    return blocked


def can_auto_apply_security(
    item: PlannedItem | dict[str, Any],
    *,
    policy: AgentPolicy,
    gates: list[str],
) -> bool:
    if not policy.enabled or not policy.auto_security:
        return False
    if gates:
        return False
    bucket = item.bucket if isinstance(item, PlannedItem) else item.get("bucket")
    needs = item.needs_confirm if isinstance(item, PlannedItem) else item.get("needs_confirm")
    return bucket == BUCKET_SECURITY and not needs


def mark_skipped_after_failure(
    items: list[dict[str, Any]],
    *,
    failed_item_id: int,
) -> list[dict[str, Any]]:
    """Stop-on-fail: leave the failed item, skip everything not already finished."""
    out: list[dict[str, Any]] = []
    for raw in items:
        item = dict(raw)
        iid = item.get("id")
        st = str(item.get("status") or "")
        if iid == failed_item_id:
            item["status"] = STATUS_FAILED
        elif st not in (STATUS_SUCCESS, STATUS_FAILED, STATUS_SKIPPED):
            item["status"] = STATUS_SKIPPED
            item["error_message"] = item.get("error_message") or (
                "Welle gestoppt nach dem ersten Apply-Fehler."
            )
        out.append(item)
    return out


def next_wave_status(
    *,
    item_ok: bool,
    remaining_runnable: int,
    remaining_waiting: int,
) -> str:
    if not item_ok:
        return WAVE_FAILED
    if remaining_runnable > 0:
        return WAVE_RUNNING
    if remaining_waiting > 0:
        return WAVE_WAITING
    return WAVE_COMPLETED


def pick_runnable_items(
    items: list[dict[str, Any]],
    *,
    max_parallel: int,
    running_target_ids: set[str],
) -> list[dict[str, Any]]:
    """At most max_parallel hosts; one item per host; already-running hosts excluded."""
    limit = max(1, int(max_parallel or 1))
    slots = max(0, limit - len(running_target_ids))
    picked: list[dict[str, Any]] = []
    seen: set[str] = set(running_target_ids)
    for item in items:
        if slots <= 0:
            break
        if str(item.get("status") or "") != STATUS_READY:
            continue
        tid = str(item.get("target_id") or "")
        if not tid or tid in seen:
            continue
        picked.append(item)
        seen.add(tid)
        slots -= 1
    return picked


def wave_banner_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    security_ready = 0
    waiting = 0
    blocked = 0
    running = 0
    failed = 0
    done = 0
    for item in items:
        st = str(item.get("status") or "")
        bucket = str(item.get("bucket") or "")
        if bucket == BUCKET_SECURITY and st in (STATUS_READY, STATUS_PLANNED):
            security_ready += 1
        elif st in (STATUS_WAITING, STATUS_PLANNED) and item.get("needs_confirm"):
            waiting += 1
        elif st == STATUS_BLOCKED:
            blocked += 1
        elif st == STATUS_RUNNING:
            running += 1
        elif st == STATUS_FAILED:
            failed += 1
        elif st == STATUS_SUCCESS:
            done += 1
        elif st == STATUS_READY:
            waiting += 1
    return {
        "security_ready": security_ready,
        "waiting": waiting,
        "blocked": blocked,
        "running": running,
        "failed": failed,
        "done": done,
        "total": len(items),
    }


def banner_text(items: list[dict[str, Any]]) -> str:
    c = wave_banner_counts(items)
    bits: list[str] = []
    if c["security_ready"]:
        bits.append(f"{c['security_ready']} Security bereit")
    if c["waiting"]:
        bits.append(f"{c['waiting']} warten auf dich")
    if c["blocked"]:
        bits.append(f"{c['blocked']} durch Gates blockiert")
    if c["running"]:
        bits.append(f"{c['running']} laufen")
    if c["failed"]:
        bits.append(f"{c['failed']} fehlgeschlagen")
    if not bits:
        if c["done"] and c["done"] == c["total"]:
            return "Welle: alle Positionen erledigt"
        return "Welle geplant — noch nichts bereit"
    return "Welle: " + ", ".join(bits)


def host_context_from_snapshot(
    snapshot: Any,
    target_id: str,
    *,
    extra_tags: list[str] | None = None,
    backup_parent_ids: set[str] | None = None,
) -> tuple[HostContext, list[str]]:
    """Build gates context + merged tags from topology (guests/hosts/nodes)."""
    tags = [str(t).strip() for t in (extra_tags or []) if str(t).strip()]
    online = True
    disk_pct: float | None = None
    entity = None
    if snapshot is not None:
        pools = []
        for attr in ("guests", "hosts", "nodes"):
            pools.extend(list(getattr(snapshot, attr, None) or []))
        for ent in pools:
            eid = getattr(ent, "id", None)
            if eid is None and isinstance(ent, dict):
                eid = ent.get("id")
            if str(eid) != str(target_id):
                continue
            entity = ent
            break
    if entity is not None:
        if isinstance(entity, dict):
            status = entity.get("status")
            meta = entity.get("meta") or {}
        else:
            status = getattr(entity, "status", None)
            meta = getattr(entity, "meta", None) or {}
        status_s = status.value if hasattr(status, "value") else str(status or "")
        if status_s and status_s not in ("running", "unknown", ""):
            online = False
        elif status_s == "running":
            online = True
        raw_pct = meta.get("disk_pct") if isinstance(meta, dict) else None
        try:
            disk_pct = float(raw_pct) if raw_pct is not None else None
        except (TypeError, ValueError):
            disk_pct = None
        if isinstance(meta, dict):
            for t in meta.get("tags_list") or []:
                if t and str(t).strip() not in tags:
                    tags.append(str(t).strip())
            raw_tags = meta.get("tags") or ""
            if isinstance(raw_tags, str):
                for part in raw_tags.replace(",", ";").split(";"):
                    p = part.strip()
                    if p and p not in tags:
                        tags.append(p)
    backup_running = str(target_id) in (backup_parent_ids or set())
    return (
        HostContext(
            target_id=target_id,
            online=online,
            disk_pct=disk_pct,
            backup_running=backup_running,
        ),
        tags,
    )


def list_backup_parent_ids() -> set[str]:
    try:
        from backup_verifier.jobs import JOBS as BACKUP_JOBS
    except Exception:
        return set()
    out: set[str] = set()
    try:
        for job in BACKUP_JOBS.list_active():
            kind = str(getattr(job, "kind", "") or "")
            if kind not in ("backup", "restore", ""):
                continue
            pid = str(getattr(job, "parent_id", "") or "")
            if pid:
                out.add(pid)
    except Exception:
        return set()
    return out


def disk_critical_threshold() -> float:
    try:
        from health.config import get_health_settings

        return float(get_health_settings().health_disk_warn_pct)
    except Exception:
        return DEFAULT_DISK_CRITICAL_PCT


def effective_policy(store_row: dict[str, Any] | None) -> AgentPolicy:
    env = get_patcher_settings()
    if not store_row:
        return AgentPolicy(
            enabled=bool(env.patcher_agent_enabled),
            auto_security=bool(env.patcher_agent_auto_security),
            max_parallel=int(env.patcher_agent_max_parallel),
        )
    enabled = store_row.get("enabled")
    auto_sec = store_row.get("auto_security")
    parallel = store_row.get("max_parallel")
    return AgentPolicy(
        enabled=bool(env.patcher_agent_enabled if enabled is None else enabled),
        auto_security=bool(
            env.patcher_agent_auto_security if auto_sec is None else auto_sec
        ),
        max_parallel=max(
            1,
            int(
                env.patcher_agent_max_parallel
                if parallel is None
                else parallel
            ),
        ),
    )


def serialize_item(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    if not (out.get("explanation") or "").strip():
        out["explanation"] = explain_wave_item(out)
    return out


class WaveEngine:
    """Persisted wave + sequential apply through existing patcher jobs."""

    def __init__(
        self,
        store: PatcherStore,
        *,
        apply_job: ApplyJobFn,
        image_apply_job: ImageApplyFn,
        get_snapshot: Callable[[], Any],
        get_inventory_tags: Callable[[str], Awaitable[list[str]]] | None = None,
    ) -> None:
        self.store = store
        self._apply_job = apply_job
        self._image_apply_job = image_apply_job
        self._get_snapshot = get_snapshot
        self._get_inventory_tags = get_inventory_tags
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def policy(self) -> AgentPolicy:
        row = await self.store.get_agent_settings()
        return effective_policy(row)

    async def save_policy(
        self,
        *,
        enabled: bool | None = None,
        auto_security: bool | None = None,
        max_parallel: int | None = None,
    ) -> AgentPolicy:
        current = await self.policy()
        next_p = AgentPolicy(
            enabled=current.enabled if enabled is None else bool(enabled),
            auto_security=(
                current.auto_security if auto_security is None else bool(auto_security)
            ),
            max_parallel=max(
                1,
                int(current.max_parallel if max_parallel is None else max_parallel),
            ),
        )
        await self.store.set_agent_settings(
            enabled=next_p.enabled,
            auto_security=next_p.auto_security,
            max_parallel=next_p.max_parallel,
        )
        return next_p

    async def current_wave(self) -> dict[str, Any] | None:
        wave = await self.store.get_latest_wave()
        if not wave:
            return None
        items = await self.store.list_wave_items(int(wave["id"]))
        packed = [serialize_item(it) for it in items]
        wave["items"] = packed
        wave["counts"] = wave_banner_counts(packed)
        wave["banner"] = banner_text(packed)
        return wave

    async def _tags_for(self, target_id: str, snapshot: Any) -> list[str]:
        extra: list[str] = []
        if self._get_inventory_tags is not None:
            try:
                extra = list(await self._get_inventory_tags(target_id))
            except Exception:
                extra = []
        _ctx, tags = host_context_from_snapshot(snapshot, target_id, extra_tags=extra)
        return tags

    async def _context_for(self, target_id: str) -> tuple[HostContext, list[str]]:
        snapshot = self._get_snapshot()
        extra: list[str] = []
        if self._get_inventory_tags is not None:
            try:
                extra = list(await self._get_inventory_tags(target_id))
            except Exception:
                extra = []
        return host_context_from_snapshot(
            snapshot,
            target_id,
            extra_tags=extra,
            backup_parent_ids=list_backup_parent_ids(),
        )

    async def plan(self, hosts: list[HostPending], *, replace: bool = True) -> dict[str, Any]:
        async with self._lock:
            current = await self.store.get_latest_wave()
            if current and current.get("status") == WAVE_RUNNING:
                raise RuntimeError("Eine Welle läuft bereits — zuerst stoppen.")
            items = group_wave(hosts)
            if not items:
                raise RuntimeError(
                    "Nichts zu planen — kein ausstehendes Update in den letzten Scans."
                )
            if current and replace and current.get("status") in (
                WAVE_PLANNED,
                WAVE_WAITING,
                WAVE_STOPPED,
                WAVE_FAILED,
                WAVE_COMPLETED,
            ):
                await self.store.delete_wave(int(current["id"]))
            rows: list[dict[str, Any]] = []
            for planned in items:
                status = STATUS_WAITING if planned.needs_confirm else STATUS_PLANNED
                row = {
                    **planned.to_row(),
                    "status": status,
                    "gates": [],
                    "explanation": explain_wave_item(
                        {**planned.to_row(), "status": status, "gates": []}
                    ),
                }
                rows.append(row)
            wave_id = await self.store.create_wave(items=rows)
            wave = await self.store.get_wave(wave_id)
            packed = [serialize_item(it) for it in await self.store.list_wave_items(wave_id)]
            assert wave is not None
            wave["items"] = packed
            wave["counts"] = wave_banner_counts(packed)
            wave["banner"] = banner_text(packed)
            return wave

    def _arm_item(
        self,
        item: dict[str, Any],
        *,
        policy: AgentPolicy,
        gates: list[str],
        force_confirm: bool = False,
    ) -> dict[str, Any]:
        out = dict(item)
        out["gates"] = gates
        planned = PlannedItem(
            target_id=str(out.get("target_id") or ""),
            target_name=str(out.get("target_name") or ""),
            bucket=str(out.get("bucket") or ""),
            needs_confirm=bool(out.get("needs_confirm")),
            confirm_reasons=list(out.get("confirm_reasons") or []),
            package_filter=str(out.get("package_filter") or "all"),
            packages=list(out.get("packages") or []),
            reason=str(out.get("reason") or ""),
        )
        if force_confirm or out.get("confirmed"):
            if gates:
                out["status"] = STATUS_BLOCKED
            else:
                out["status"] = STATUS_READY
                out["confirmed"] = True
        elif can_auto_apply_security(planned, policy=policy, gates=gates):
            out["status"] = STATUS_READY
        elif planned.needs_confirm or not policy.auto_security or planned.bucket != BUCKET_SECURITY:
            out["status"] = STATUS_WAITING
        else:
            out["status"] = STATUS_BLOCKED
        out["explanation"] = explain_wave_item(out)
        return out

    async def start(self) -> dict[str, Any]:
        policy = await self.policy()
        if not policy.enabled:
            raise RuntimeError("Wellen-Agent ist aus. Unter Zeitplan oder per Env einschalten.")
        async with self._lock:
            wave = await self.store.get_latest_wave()
            if not wave:
                raise RuntimeError("Keine Welle geplant. Zuerst „Welle planen“.")
            if wave.get("status") == WAVE_RUNNING:
                raise RuntimeError("Welle läuft bereits.")
            if wave.get("status") in (WAVE_FAILED, WAVE_STOPPED, WAVE_COMPLETED):
                raise RuntimeError("Diese Welle ist beendet. Bitte neu planen.")
            items = await self.store.list_wave_items(int(wave["id"]))
            if not items:
                raise RuntimeError("Welle hat keine Positionen.")
            threshold = disk_critical_threshold()
            updated: list[dict[str, Any]] = []
            for item in items:
                if str(item.get("status") or "") in (
                    STATUS_SUCCESS,
                    STATUS_FAILED,
                    STATUS_SKIPPED,
                    STATUS_RUNNING,
                ):
                    updated.append(item)
                    continue
                ctx, _tags = await self._context_for(str(item.get("target_id") or ""))
                gates = evaluate_gates(ctx, disk_critical_pct=threshold)
                updated.append(self._arm_item(item, policy=policy, gates=gates))
            for item in updated:
                await self.store.update_wave_item(
                    int(item["id"]),
                    status=str(item["status"]),
                    gates=item.get("gates") or [],
                    confirmed=bool(item.get("confirmed")),
                    explanation=item.get("explanation") or "",
                )
            await self.store.update_wave(int(wave["id"]), status=WAVE_RUNNING)
        self._kick()
        result = await self.current_wave()
        assert result is not None
        return result

    async def stop(self, *, reason: str = "Vom Operator gestoppt.") -> dict[str, Any]:
        async with self._lock:
            wave = await self.store.get_latest_wave()
            if not wave:
                raise RuntimeError("Keine Welle vorhanden.")
            items = await self.store.list_wave_items(int(wave["id"]))
            for item in items:
                st = str(item.get("status") or "")
                if st in (STATUS_SUCCESS, STATUS_FAILED, STATUS_SKIPPED, STATUS_RUNNING):
                    continue
                expl = explain_wave_item({**item, "status": STATUS_SKIPPED})
                await self.store.update_wave_item(
                    int(item["id"]),
                    status=STATUS_SKIPPED,
                    error_message=reason,
                    explanation=expl,
                )
            await self.store.update_wave(
                int(wave["id"]),
                status=WAVE_STOPPED,
                stop_reason=reason,
            )
        result = await self.current_wave()
        assert result is not None
        return result

    async def confirm(
        self,
        *,
        item_ids: list[int] | None = None,
        all_waiting: bool = False,
    ) -> dict[str, Any]:
        policy = await self.policy()
        if not policy.enabled:
            raise RuntimeError("Wellen-Agent ist aus.")
        async with self._lock:
            wave = await self.store.get_latest_wave()
            if not wave:
                raise RuntimeError("Keine Welle geplant.")
            if wave.get("status") in (WAVE_STOPPED, WAVE_FAILED, WAVE_COMPLETED):
                raise RuntimeError("Diese Welle ist beendet. Bitte neu planen.")
            items = await self.store.list_wave_items(int(wave["id"]))
            wanted: set[int] | None = None
            if not all_waiting:
                wanted = {int(i) for i in (item_ids or []) if i}
                if not wanted:
                    raise RuntimeError("Keine Position zum Bestätigen.")
            threshold = disk_critical_threshold()
            confirmed_n = 0
            for item in items:
                iid = int(item["id"])
                st = str(item.get("status") or "")
                if st in (STATUS_SUCCESS, STATUS_FAILED, STATUS_SKIPPED, STATUS_RUNNING):
                    continue
                if wanted is not None and iid not in wanted:
                    continue
                if wanted is None and not (
                    item.get("needs_confirm")
                    or st in (STATUS_WAITING, STATUS_BLOCKED, STATUS_PLANNED)
                ):
                    continue
                ctx, _tags = await self._context_for(str(item.get("target_id") or ""))
                gates = evaluate_gates(ctx, disk_critical_pct=threshold)
                armed = self._arm_item(
                    {**item, "confirmed": True, "needs_confirm": item.get("needs_confirm")},
                    policy=policy,
                    gates=gates,
                    force_confirm=True,
                )
                await self.store.update_wave_item(
                    iid,
                    status=str(armed["status"]),
                    gates=armed.get("gates") or [],
                    confirmed=True,
                    explanation=armed.get("explanation") or "",
                    error_message="" if armed["status"] == STATUS_READY else (
                        " ".join(armed.get("gates") or [])
                    ),
                )
                confirmed_n += 1
            if confirmed_n == 0:
                raise RuntimeError("Keine passenden Positionen zum Bestätigen.")
            if wave.get("status") in (WAVE_PLANNED, WAVE_WAITING, WAVE_STOPPED):
                await self.store.update_wave(int(wave["id"]), status=WAVE_RUNNING)
        self._kick()
        result = await self.current_wave()
        assert result is not None
        return result

    def _kick(self) -> None:
        task = self._task
        if task is not None and not task.done():
            return
        self._task = asyncio.create_task(self._run_loop(), name="patcher-wave")

    async def resume_if_needed(self) -> None:
        wave = await self.store.get_latest_wave()
        if not wave:
            return
        if wave.get("status") != WAVE_RUNNING:
            return
        items = await self.store.list_wave_items(int(wave["id"]))
        orphan = False
        for item in items:
            if str(item.get("status") or "") != STATUS_RUNNING:
                continue
            job_id = str(item.get("job_id") or "")
            job = JOBS.get(job_id) if job_id else None
            if job and job.status in ("queued", "running"):
                continue
            if job and job.status == "success":
                expl = explain_wave_item({**item, "status": STATUS_SUCCESS})
                await self.store.update_wave_item(
                    int(item["id"]),
                    status=STATUS_SUCCESS,
                    explanation=expl,
                )
                continue
            orphan = True
            msg = (
                "Auftrag nach Neustart nicht mehr im Speicher — "
                "bitte apt/dpkg auf dem Host prüfen. Kein automatischer Retry."
            )
            expl = explain_wave_item(
                {**item, "status": STATUS_FAILED, "error_message": msg}
            )
            await self.store.update_wave_item(
                int(item["id"]),
                status=STATUS_FAILED,
                error_message=msg,
                explanation=expl,
            )
        if orphan:
            latest = await self.store.list_wave_items(int(wave["id"]))
            skipped = mark_skipped_after_failure(
                latest,
                failed_item_id=next(
                    (
                        int(i["id"])
                        for i in latest
                        if i.get("status") == STATUS_FAILED
                    ),
                    int(items[0]["id"]),
                ),
            )
            for item in skipped:
                await self.store.update_wave_item(
                    int(item["id"]),
                    status=str(item["status"]),
                    error_message=item.get("error_message") or "",
                    explanation=explain_wave_item(item),
                )
            await self.store.update_wave(
                int(wave["id"]),
                status=WAVE_FAILED,
                stop_reason="Apply-Job nach Container-Neustart verloren.",
            )
            return
        self._kick()

    async def _run_loop(self) -> None:
        try:
            while True:
                progressed = await self._step()
                if not progressed:
                    return
        except Exception:
            logger.exception("Wellen-Lauf fehlgeschlagen")

    async def _step(self) -> bool:
        policy = await self.policy()
        async with self._lock:
            wave = await self.store.get_latest_wave()
            if not wave or wave.get("status") != WAVE_RUNNING:
                return False
            items = await self.store.list_wave_items(int(wave["id"]))
            running = [
                it
                for it in items
                if str(it.get("status") or "") == STATUS_RUNNING
            ]
            runnable = pick_runnable_items(
                items,
                max_parallel=policy.max_parallel,
                running_target_ids={str(it.get("target_id") or "") for it in running},
            )
            waiting_n = sum(
                1
                for it in items
                if str(it.get("status") or "") in (STATUS_WAITING, STATUS_BLOCKED)
            )
            ready_n = sum(1 for it in items if str(it.get("status") or "") == STATUS_READY)
            if running:
                return False
            if not runnable:
                nxt = next_wave_status(
                    item_ok=True,
                    remaining_runnable=ready_n,
                    remaining_waiting=waiting_n,
                )
                if nxt != WAVE_RUNNING:
                    await self.store.update_wave(int(wave["id"]), status=nxt)
                return False
            for chosen in runnable:
                await self.store.update_wave_item(int(chosen["id"]), status=STATUS_RUNNING)

        results = await asyncio.gather(
            *[self._execute_item(item) for item in runnable],
            return_exceptions=True,
        )
        async with self._lock:
            wave = await self.store.get_latest_wave()
            if not wave:
                return False
            items = await self.store.list_wave_items(int(wave["id"]))
            failed_id: int | None = None
            fail_msg = ""
            for chosen, result in zip(runnable, results, strict=True):
                if isinstance(result, Exception):
                    ok, error, job_id = False, str(result), None
                else:
                    ok, error, job_id = result
                if ok:
                    expl = explain_wave_item(
                        {**chosen, "status": STATUS_SUCCESS, "job_id": job_id}
                    )
                    await self.store.update_wave_item(
                        int(chosen["id"]),
                        status=STATUS_SUCCESS,
                        job_id=job_id,
                        explanation=expl,
                    )
                    continue
                if failed_id is None:
                    failed_id = int(chosen["id"])
                    fail_msg = error or "Apply fehlgeschlagen."
                await self.store.update_wave_item(
                    int(chosen["id"]),
                    status=STATUS_FAILED,
                    job_id=job_id,
                    error_message=error or "Apply fehlgeschlagen.",
                    explanation=explain_wave_item(
                        {**chosen, "status": STATUS_FAILED, "error_message": error}
                    ),
                )
            if failed_id is not None:
                latest = await self.store.list_wave_items(int(wave["id"]))
                skipped = mark_skipped_after_failure(latest, failed_item_id=failed_id)
                for item in skipped:
                    expl = explain_wave_item(item)
                    await self.store.update_wave_item(
                        int(item["id"]),
                        status=str(item["status"]),
                        error_message=item.get("error_message") or "",
                        explanation=expl,
                    )
                    if self._should_enrich():
                        asyncio.create_task(
                            self._enrich_item(int(item["id"]), item),
                            name="patcher-wave-explain",
                        )
                await self.store.update_wave(
                    int(wave["id"]),
                    status=WAVE_FAILED,
                    stop_reason=fail_msg,
                    failed_item_id=failed_id,
                )
                return False

            items = await self.store.list_wave_items(int(wave["id"]))
            ready_n = sum(1 for it in items if str(it.get("status") or "") == STATUS_READY)
            waiting_n = sum(
                1
                for it in items
                if str(it.get("status") or "") in (STATUS_WAITING, STATUS_BLOCKED)
            )
            nxt = next_wave_status(
                item_ok=True,
                remaining_runnable=ready_n,
                remaining_waiting=waiting_n,
            )
            await self.store.update_wave(int(wave["id"]), status=nxt)
            return nxt == WAVE_RUNNING

    def _should_enrich(self) -> bool:
        try:
            return bool(get_patcher_settings().llm_configured)
        except Exception:
            return False

    async def _enrich_item(self, item_id: int, item: dict[str, Any]) -> None:
        try:
            text = await maybe_enrich_explanation(
                str(item.get("explanation") or explain_wave_item(item)),
                context=item,
            )
            await self.store.update_wave_item(item_id, explanation=text)
        except Exception:
            logger.info("Wellen-Erklärung nicht angereichert", exc_info=True)

    async def execute_ops_item(
        self,
        *,
        target_id: str,
        target_name: str,
        bucket: str,
        packages: list[str] | None = None,
    ) -> tuple[bool, str, str | None]:
        """Apply one host inside an ops-agent window. Never DistUpgrade."""
        filt = "images" if bucket == BUCKET_IMAGES else (
            "security" if bucket == BUCKET_SECURITY else "selected"
        )
        item = {
            "id": 0,
            "target_id": target_id,
            "target_name": target_name,
            "bucket": bucket,
            "package_filter": filt,
            "packages": list(packages or []),
        }
        return await self._execute_item(item)

    async def _execute_item(
        self, item: dict[str, Any]
    ) -> tuple[bool, str, str | None]:
        target_id = str(item.get("target_id") or "")
        bucket = str(item.get("bucket") or "")
        filt = str(item.get("package_filter") or "security")
        packages = [str(p) for p in (item.get("packages") or []) if p]
        job_kind = "image-apply" if bucket == BUCKET_IMAGES else "apply"
        job = JOBS.create(kind=job_kind, target_id=target_id, via_agent=True)
        JOBS.set_progress(
            job.id,
            phase="Welle",
            percent=4,
            message=f"Wellen-Position: {_bucket_de(bucket)} auf {item.get('target_name') or target_id}",
        )
        JOBS.append_log(
            job.id,
            explain_wave_item({**item, "status": STATUS_RUNNING, "via_agent": True}),
        )
        try:
            iid = int(item.get("id") or 0)
            if iid > 0:
                await self.store.update_wave_item(iid, job_id=job.id)
        except Exception:
            logger.exception("job_id an Wellen-Position speichern fehlgeschlagen")

        snapshot = self._get_snapshot()
        try:
            if bucket == BUCKET_IMAGES:
                names = packages
                if not names:
                    latest = await self.store.latest_image_scan_for_target(
                        target_id, success_only=True
                    )
                    raw = ((latest or {}).get("summary") or {}).get("updates") or []
                    names = [
                        str(u.get("name"))
                        for u in raw
                        if isinstance(u, dict) and u.get("name")
                    ]
                if not names:
                    raise RuntimeError(
                        "Keine Image-Updates zum Einspielen (bitte zuerst Images prüfen)."
                    )
                await self._image_apply_job(
                    job.id,
                    target_id=target_id,
                    snapshot=snapshot,
                    names=names,
                    restart=True,
                    prune=False,
                )
            else:
                await self._apply_job(
                    job.id,
                    target_id=target_id,
                    snapshot=snapshot,
                    package_filter=filt if filt in ("security", "all", "selected") else "security",
                    packages=packages if filt == "selected" else [],
                    reboot_after=False,
                    snapshot_first=True,
                    proceed_without_snapshot=False,
                )
        except Exception as exc:
            msg = getattr(exc, "message", None) or str(exc)
            JOBS.finish(job.id, status="failed", error=msg)
            return False, msg, job.id

        done = JOBS.get(job.id)
        if done is None:
            return False, "Apply-Job nicht mehr im Speicher.", job.id
        if done.status != "success":
            return False, (done.error or done.message or "Apply fehlgeschlagen."), job.id
        return True, "", job.id


def _bucket_de(bucket: str) -> str:
    return {
        BUCKET_SECURITY: "Security",
        BUCKET_REGULAR: "Bestätigung",
        BUCKET_IMAGES: "Images",
    }.get(bucket, bucket)


async def hosts_from_store(
    store: PatcherStore,
    snapshot: Any,
    *,
    tags_for: Callable[[str], Awaitable[list[str]]] | None = None,
) -> list[HostPending]:
    """Build pending-work hosts from latest successful scans (monitored only)."""
    from patcher.targets import list_targets

    targets = await list_targets(store, snapshot)
    excluded = await store.list_unmonitored_ids()
    out: list[HostPending] = []
    for target in targets:
        if target.id in excluded:
            continue
        latest = await store.latest_scan_for_target(target.id)
        packages: list[dict[str, Any]] = []
        if latest and latest.get("status") == "success" and latest.get("id"):
            packages = await store.list_packages(int(latest["id"]))
        img = await store.latest_image_scan_for_target(target.id, success_only=True)
        image_names: list[str] = []
        image_count = 0
        if img and img.get("status") == "success":
            image_count = int(img.get("update_count") or 0)
            raw = ((img.get("summary") or {}).get("updates") or [])
            image_names = [
                str(u.get("name"))
                for u in raw
                if isinstance(u, dict) and u.get("name")
            ]
        tags: list[str] = []
        if tags_for is not None:
            try:
                tags = list(await tags_for(target.id))
            except Exception:
                tags = []
        _ctx, merged = host_context_from_snapshot(snapshot, target.id, extra_tags=tags)
        if not packages and image_count <= 0 and not image_names:
            continue
        out.append(
            HostPending(
                target_id=target.id,
                target_name=target.name,
                packages=packages,
                image_updates=image_count,
                image_names=image_names,
                tags=merged,
            )
        )
    return out
