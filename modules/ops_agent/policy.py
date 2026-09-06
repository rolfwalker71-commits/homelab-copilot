"""Confirmation interview — the agent asks once, then runs itself.

Hard stops (never autonomous, never offered as a toggle): DistUpgrade,
wipe, power-cycle, restore-to-prod. Those stay on their existing confirm UIs.

Until the operator answers, defaults match the pre-checked interview:
autonomous for normal backups and security patches on already-known hosts;
confirm kernel/docker and a first backup window on a new guest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROD_TAGS = ("prod", "production", "produktion")

HARD_STOPS = ("distupgrade", "release-upgrade", "wipe", "power-cycle", "restore-to-prod")


@dataclass
class ConfirmPolicy:
    answered: bool = False
    confirm_kernel_docker: bool = True
    confirm_new_guest_backup: bool = True
    confirm_production: bool = False
    confirm_nothing: bool = False
    production_tags: list[str] = field(default_factory=lambda: list(PROD_TAGS))
    focus_mode: str = "all"  # legacy; host matrix is the source of truth
    focus_ids: list[str] = field(default_factory=list)
    focus_tags: list[str] = field(default_factory=list)
    patch_scope_ids: list[str] = field(default_factory=list)
    image_scope_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answered": self.answered,
            "confirm_kernel_docker": self.confirm_kernel_docker,
            "confirm_new_guest_backup": self.confirm_new_guest_backup,
            "confirm_production": self.confirm_production,
            "confirm_nothing": self.confirm_nothing,
            "production_tags": list(self.production_tags),
            "focus_mode": self.focus_mode,
            "focus_ids": list(self.focus_ids),
            "focus_tags": list(self.focus_tags),
            "patch_scope_ids": list(self.patch_scope_ids),
            "image_scope_ids": list(self.image_scope_ids),
            "defaults_note": DEFAULTS_NOTE,
            "hard_stops": list(HARD_STOPS),
        }


DEFAULTS_NOTE = (
    "Standard, bis du etwas änderst: Backups und Security auf bekannten Hosts "
    "laufen selbst — aber nur auf Hosts, die du in der Liste für Patchen bzw. "
    "Images anhakst. Kernel, Docker-Engine und das erste Backup-Fenster auf einem "
    "neuen Gast warten auf dich. DistUpgrade, Wipe, Power-Cycle und Restore-auf-Prod "
    "macht der Agent nie."
)


def default_policy() -> ConfirmPolicy:
    """Pre-checked interview defaults — also used until the card is saved."""
    return ConfirmPolicy()


def policy_from_row(row: dict[str, Any] | None) -> ConfirmPolicy:
    if not row:
        return default_policy()
    tags = row.get("production_tags")
    if not isinstance(tags, list) or not tags:
        tags = list(PROD_TAGS)
    mode = str(row.get("focus_mode") or "all")
    if mode not in ("all", "only", "exclude"):
        mode = "all"
    return ConfirmPolicy(
        answered=bool(row.get("answered")),
        confirm_kernel_docker=bool(row.get("confirm_kernel_docker", True)),
        confirm_new_guest_backup=bool(row.get("confirm_new_guest_backup", True)),
        confirm_production=bool(row.get("confirm_production", False)),
        confirm_nothing=bool(row.get("confirm_nothing", False)),
        production_tags=[str(t).strip().lower() for t in tags if str(t).strip()],
        focus_mode=mode,
        focus_ids=[str(x).strip() for x in (row.get("focus_ids") or []) if str(x).strip()],
        focus_tags=[
            str(t).strip().lower()
            for t in (row.get("focus_tags") or [])
            if str(t).strip()
        ],
        patch_scope_ids=_norm_ids(row.get("patch_scope_ids")),
        image_scope_ids=_norm_ids(row.get("image_scope_ids")),
    )


def apply_interview(
    current: ConfirmPolicy,
    *,
    confirm_kernel_docker: bool,
    confirm_new_guest_backup: bool,
    confirm_production: bool,
    confirm_nothing: bool,
    production_tags: list[str] | None = None,
    focus_mode: str | None = None,
    focus_ids: list[str] | None = None,
    focus_tags: list[str] | None = None,
    patch_scope_ids: list[str] | None = None,
    image_scope_ids: list[str] | None = None,
) -> ConfirmPolicy:
    """Interview answers must not wipe host scope unless lists are sent."""
    mode = str(focus_mode or current.focus_mode or "all")
    if mode not in ("all", "only", "exclude"):
        mode = "all"
    tags = production_tags if production_tags is not None else current.production_tags
    if not isinstance(tags, list) or not tags:
        tags = list(PROD_TAGS)
    return ConfirmPolicy(
        answered=True,
        confirm_kernel_docker=bool(confirm_kernel_docker),
        confirm_new_guest_backup=bool(confirm_new_guest_backup),
        confirm_production=bool(confirm_production),
        confirm_nothing=bool(confirm_nothing),
        production_tags=[str(t).strip().lower() for t in tags if str(t).strip()],
        focus_mode=mode,
        focus_ids=(
            [str(x).strip() for x in focus_ids if str(x).strip()]
            if focus_ids is not None
            else list(current.focus_ids)
        ),
        focus_tags=(
            [str(t).strip().lower() for t in focus_tags if str(t).strip()]
            if focus_tags is not None
            else list(current.focus_tags)
        ),
        patch_scope_ids=(
            _norm_ids(patch_scope_ids)
            if patch_scope_ids is not None
            else list(current.patch_scope_ids)
        ),
        image_scope_ids=(
            _norm_ids(image_scope_ids)
            if image_scope_ids is not None
            else list(current.image_scope_ids)
        ),
    )


def _norm_ids(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tid = str(item).strip()
        key = tid.lower()
        if not tid or key in seen:
            continue
        seen.add(key)
        out.append(tid)
    return out


def _norm_tags(tags: list[str] | None) -> set[str]:
    return {str(t).strip().lower() for t in (tags or []) if str(t).strip()}


def in_id_list(ids: list[str], target_id: str) -> bool:
    """Empty list is never a match — no silent 'all hosts'."""
    wanted = {str(x).strip().lower() for x in ids if str(x).strip()}
    if not wanted:
        return False
    return str(target_id or "").strip().lower() in wanted


def in_job_scope(
    policy: ConfirmPolicy,
    *,
    kind: str,
    bucket: str = "",
    target_id: str = "",
    gone_ids: set[str] | None = None,
) -> bool:
    """Patch and image windows use separate persisted lists. Empty = no autonomous wave."""
    kind_n = str(kind or "").strip().lower()
    bucket_n = str(bucket or "").strip().lower()
    tid = str(target_id or "").strip()
    gone = {str(x).strip().lower() for x in (gone_ids or set()) if str(x).strip()}
    if tid and tid.lower() in gone:
        return False
    if kind_n in ("backup", "drill", "restore"):
        return True
    if bucket_n == "images" or kind_n == "image":
        return in_id_list(policy.image_scope_ids, tid)
    if kind_n == "patch":
        return in_id_list(policy.patch_scope_ids, tid)
    return True


def in_focus(policy: ConfirmPolicy, *, target_id: str, tags: list[str] | None) -> bool:
    """Whether this host/guest is in the agent's working set."""
    tid = str(target_id or "").strip()
    host_tags = _norm_tags(tags)
    wanted_ids = {x.lower() for x in policy.focus_ids}
    wanted_tags = {t.lower() for t in policy.focus_tags}
    matched = False
    if wanted_ids and tid.lower() in wanted_ids:
        matched = True
    if wanted_tags and host_tags & wanted_tags:
        matched = True
    if not wanted_ids and not wanted_tags:
        matched = policy.focus_mode == "all"
    if policy.focus_mode == "all":
        return True
    if policy.focus_mode == "only":
        return matched
    if policy.focus_mode == "exclude":
        return not matched
    return True


def has_production_tag(tags: list[str] | None, production_tags: list[str]) -> bool:
    host = _norm_tags(tags)
    return bool(host & {t.lower() for t in production_tags})


def is_hard_stop(kind: str | None) -> bool:
    raw = str(kind or "").strip().lower()
    return raw in HARD_STOPS or raw.replace("_", "-") in HARD_STOPS


def needs_human(
    policy: ConfirmPolicy,
    *,
    kind: str,
    bucket: str = "",
    confirm_reasons: list[str] | None = None,
    tags: list[str] | None = None,
    has_existing_schedule: bool = True,
    known_host: bool = True,
) -> tuple[bool, list[str]]:
    """Return (wait_for_operator, reasons). Hard stops are not planned here."""
    if is_hard_stop(kind):
        return True, ["hard-stop"]
    if policy.confirm_nothing:
        return False, []

    reasons = [str(r) for r in (confirm_reasons or []) if r]
    wait: list[str] = []

    if policy.confirm_production and has_production_tag(tags, policy.production_tags):
        wait.append("production")

    if kind == "patch":
        if policy.confirm_kernel_docker:
            if "kernel" in reasons or "docker" in reasons:
                wait.append("kernel-docker")
        if bucket == "images" and policy.confirm_kernel_docker:
            wait.append("images")
        if "no-auto-patch" in reasons:
            wait.append("no-auto-patch")
        if not known_host and bucket == "security":
            wait.append("neuer-host")

    if kind == "backup":
        if policy.confirm_new_guest_backup and not has_existing_schedule:
            wait.append("erstes-backup")

    return (bool(wait), wait)
