"""Backup-verifier settings (env + DATA_DIR)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import get_settings as get_app_settings
from app.core.docker_control import ssh_key_path


class BackupSettings(BaseSettings):
    """Module-local backup configuration.

    Runtime destinations (order, SFTP credentials, keep-N) live in SQLite via the
    Ziele UI. Env values seed the DB on first start and still supply local paths
    (BACKUP_COPILOT_DIR / BACKUP_LXC_DIR) and timeouts.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    backup_copilot_dir: str = ""  # default: DATA_DIR/backups
    backup_lxc_dir: str = "/var/backups/homelab-copilot"
    backup_lxc_keep: int = Field(default=2, ge=1, le=50)
    backup_copilot_keep: int = Field(default=5, ge=1, le=100)
    backup_synology_keep: int = Field(default=10, ge=1, le=200)
    backup_synology_host: str = ""
    backup_synology_user: str = ""
    backup_synology_path: str = ""
    backup_synology_key_path: str = ""  # empty → reuse Docker SSH key
    backup_synology_port: int = 22
    backup_quiesce: bool = True
    # Short SSH ops (compose stop/start, mkdir, status polls)
    backup_ssh_timeout: float = Field(default=120.0, ge=30.0, le=3600.0)
    # Wall-clock for remote archive/extract jobs (tar of volumes/binds)
    backup_archive_timeout: float = Field(default=3600.0, ge=300.0, le=14400.0)
    # SCP / large file hops (LXC ↔ Copilot ↔ Synology)
    backup_transfer_timeout: float = Field(default=3600.0, ge=300.0, le=14400.0)
    backup_api_base: str = "http://127.0.0.1:6655"  # legacy; schedules are in-process

    @property
    def copilot_dir(self) -> Path:
        if self.backup_copilot_dir:
            return Path(self.backup_copilot_dir)
        return Path(get_app_settings().data_dir) / "backups"

    @property
    def db_path(self) -> Path:
        return Path(get_app_settings().data_dir) / "backup_verifier.db"

    @property
    def synology_configured(self) -> bool:
        return bool(
            self.backup_synology_host
            and self.backup_synology_user
            and self.backup_synology_path
        )

    def synology_key(self) -> Path:
        if self.backup_synology_key_path:
            p = Path(self.backup_synology_key_path)
            if p.is_file():
                return p
        return ssh_key_path(get_app_settings())


@lru_cache
def get_backup_settings() -> BackupSettings:
    return BackupSettings()
