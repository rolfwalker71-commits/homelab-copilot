"""Ops-Agent settings (env + DATA_DIR)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import get_settings as get_app_settings


class OpsAgentSettings(BaseSettings):
    """Planning/execute loop. Env defaults stay off so prod does not start overnight."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # false = ingest/board only until UI or env turns the loop on.
    ops_agent_enabled: bool = False
    # After a plan is accepted (or auto-applied), shift later windows without asking.
    ops_agent_shift_auto: bool = True
    # Quiet hours for NEW proposals (Europe/Berlin). See planner.py.
    ops_agent_quiet_start: str = "20:00"
    ops_agent_quiet_end: str = "23:50"

    @property
    def db_path(self) -> Path:
        return Path(get_app_settings().data_dir) / "ops_agent.db"


@lru_cache
def get_ops_settings() -> OpsAgentSettings:
    return OpsAgentSettings()
