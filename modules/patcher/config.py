"""Patcher settings (env + DATA_DIR)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import get_settings as get_app_settings


class PatcherSettings(BaseSettings):
    """Module-local patch configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    patcher_scan_timeout: float = Field(default=120.0, ge=30.0, le=600.0)
    patcher_apply_timeout: float = Field(default=3600.0, ge=300.0, le=14400.0)
    patcher_connect_timeout: float = Field(default=15.0, ge=3.0, le=60.0)
    patcher_api_base: str = "http://127.0.0.1:6655"

    # In-process daily scan of all hosts (works inside Docker; no host crontab needed)
    patcher_daily_enabled: bool = True
    # Hour in Europe/Berlin (0–23). Ignored when PATCHER_CRON is set.
    patcher_daily_hour: int = Field(default=4, ge=0, le=23)
    # Optional 5-field cron: "m h dom mon dow" (e.g. "0 4 * * *")
    patcher_cron: str = ""

    # OpenAI-compatible chat API (Ollama: http://127.0.0.1:11434/v1)
    patcher_llm_api_key: str = ""
    patcher_llm_base_url: str = "https://api.openai.com/v1"
    patcher_llm_model: str = "gpt-4o-mini"
    patcher_llm_timeout: float = Field(default=60.0, ge=5.0, le=300.0)

    @property
    def db_path(self) -> Path:
        return Path(get_app_settings().data_dir) / "patcher.db"

    @property
    def llm_configured(self) -> bool:
        return bool(self.patcher_llm_api_key.strip())


@lru_cache
def get_patcher_settings() -> PatcherSettings:
    return PatcherSettings()
