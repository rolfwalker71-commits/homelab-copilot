"""Health-check settings (poll interval only)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import get_settings as get_app_settings


class HealthSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    health_poll_interval_seconds: int = Field(default=300, ge=60, le=3600)
    health_http_timeout: float = Field(default=12.0, ge=3.0, le=60.0)
    health_cert_warn_days: int = Field(default=14, ge=1, le=90)
    health_disk_warn_pct: float = Field(default=90.0, ge=70.0, le=99.0)

    @property
    def db_path(self) -> Path:
        return Path(get_app_settings().data_dir) / "health.db"


@lru_cache
def get_health_settings() -> HealthSettings:
    return HealthSettings()
