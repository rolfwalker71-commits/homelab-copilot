"""Application settings loaded from environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for Homelab Operations Copilot."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core ---
    app_name: str = "Homelab Operations Copilot"
    app_version: str = "0.1.0"
    host: str = "0.0.0.0"
    port: int = 6655
    debug: bool = False
    data_dir: Path = Path("/data")
    modules_dir: Path = Path("/app/modules")

    # --- Discovery refresh ---
    discovery_interval_seconds: int = Field(default=300, ge=30)

    # --- Proxmox ---
    proxmox_host: str = ""
    proxmox_port: int = 8006
    proxmox_user: str = "root@pam"
    proxmox_token_id: str = ""
    proxmox_token_secret: str = ""
    proxmox_password: str = ""
    proxmox_verify_ssl: bool = False
    proxmox_node: str = ""  # optional: limit to one node

    # --- Docker discovery ---
    # Prefer local socket when the copilot itself runs with docker.sock mounted.
    docker_socket: str = "/var/run/docker.sock"
    docker_use_local_socket: bool = True
    # SSH key used to reach remote LXC/hosts that expose Docker.
    docker_ssh_user: str = "root"
    docker_ssh_key_path: str = "/data/ssh/id_ed25519"
    docker_ssh_port: int = 22
    # Per-host connect+command budget (unreachable hosts should fail fast).
    docker_ssh_timeout: float = Field(default=3.0, ge=0.5, le=30.0)
    docker_ssh_concurrency: int = Field(default=4, ge=1, le=16)
    # Cap total SSH scan time so POST /discovery/refresh stays browser-friendly.
    docker_ssh_budget_seconds: float = Field(default=25.0, ge=5.0, le=120.0)

    # --- TOTP site gate ---
    totp_cookie_days: int = Field(default=30, ge=1, le=365)
    totp_issuer: str = "HomelabOps"
    # auto | true | false — Secure cookie flag (auto = HTTPS / X-Forwarded-Proto)
    totp_cookie_secure: str = "auto"

    # --- Web Push (optional env override; else generated into DATA_DIR SQLite) ---
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:admin@localhost"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "topology.db"

    @property
    def app_db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def inventory_db_path(self) -> Path:
        return self.data_dir / "inventory.db"

    @property
    def proxmox_configured(self) -> bool:
        has_auth = bool(self.proxmox_token_secret) or bool(self.proxmox_password)
        return bool(self.proxmox_host) and has_auth

    @property
    def proxmox_base_url(self) -> str:
        return f"https://{self.proxmox_host}:{self.proxmox_port}/api2/json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
