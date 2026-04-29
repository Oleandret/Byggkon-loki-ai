"""Application configuration via pydantic-settings.

All values are read from environment variables (or an .env file in dev).
See .env.example for the full list and documentation.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SyncScope(str, Enum):
    ALL_USERS = "all_users"
    ALL_USERS_AND_SHAREPOINT = "all_users_and_sharepoint"
    USERS_CSV = "users_csv"
    DRIVES_CSV = "drives_csv"


class UnstructuredStrategy(str, Enum):
    AUTO = "auto"
    FAST = "fast"
    HI_RES = "hi_res"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Microsoft Graph (empty defaults so the app can boot to admin UI;
    # the user fills these in via the admin or env vars before sync runs)
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_authority: str = "https://login.microsoftonline.com"
    graph_scope: str = "https://graph.microsoft.com/.default"
    graph_base_url: str = "https://graph.microsoft.com/v1.0"

    # Sync scope
    sync_scope: SyncScope = SyncScope.ALL_USERS
    sync_users: str = ""  # comma-separated UPNs
    sync_drive_ids: str = ""  # comma-separated drive IDs
    sync_path_prefix: str = ""
    max_file_bytes: int = 50 * 1024 * 1024

    # Scheduler
    sync_interval_minutes: int = 10
    sync_cron: str = ""
    sync_on_startup: bool = True

    # OpenAI (optional default so admin UI can come up first)
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-large"
    openai_embedding_dimensions: int = 3072
    embedding_batch_size: int = 64

    # Pinecone (optional default so admin UI can come up first)
    pinecone_api_key: str = ""
    pinecone_index: str = ""
    pinecone_namespace: str = ""

    # Unstructured
    unstructured_strategy: UnstructuredStrategy = UnstructuredStrategy.AUTO
    unstructured_chunk_max_chars: int = 1500
    unstructured_chunk_overlap: int = 150

    # Storage
    state_dir: str = "/data"
    tmp_dir: str = "/tmp/onedrive-sync"

    # Runtime
    log_level: str = "INFO"
    port: int = 8000
    process_concurrency: int = 2

    # Admin UI
    admin_password: str = ""  # if empty, admin UI is locked (no login possible)
    admin_session_secret: str = ""  # set to a long random string in prod
    admin_session_hours: int = 12

    # Branding (UI customisation; also editable from the admin)
    brand_name: str = "Loki AI"
    brand_owner: str = "Byggkon"

    @field_validator("sync_users", "sync_drive_ids", mode="before")
    @classmethod
    def _strip(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v).strip()

    def users_list(self) -> list[str]:
        return [u.strip() for u in self.sync_users.split(",") if u.strip()]

    def drive_ids_list(self) -> list[str]:
        return [d.strip() for d in self.sync_drive_ids.split(",") if d.strip()]

    @property
    def graph_authority_url(self) -> str:
        return f"{self.graph_authority}/{self.graph_tenant_id}"


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


def reload_settings(overrides: dict[str, object] | None = None) -> Settings:
    """Replace the cached Settings with a fresh one, optionally applying
    a dict of overrides (these win over env). Used by the settings UI to
    reload without restarting the process."""
    global _settings
    base = Settings().model_dump()  # type: ignore[call-arg]
    if overrides:
        for k, v in overrides.items():
            if v is not None and k in base:
                base[k] = v
    _settings = Settings(**base)  # type: ignore[arg-type]
    return _settings
