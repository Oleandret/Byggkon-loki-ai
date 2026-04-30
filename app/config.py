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


class EmbeddingProvider(str, Enum):
    OPENAI = "openai"
    GEMINI = "gemini"
    BOTH = "both"  # write to both providers' indexes in parallel


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
    sync_path_prefix: str = ""  # legacy single prefix; see include/exclude below
    sync_include_paths: str = ""  # newline- or comma-separated path prefixes to include
    sync_exclude_paths: str = ""  # newline- or comma-separated path prefixes to skip
    sync_folder_selections: str = ""  # JSON: {"user@x": ["/Documents", ...]}
    max_file_bytes: int = 50 * 1024 * 1024

    # Scheduler
    sync_interval_minutes: int = 10
    sync_cron: str = ""
    sync_on_startup: bool = True

    # ─── Embedding provider selector ─────────────────────────────────
    # 'openai'  → write only to OpenAI index
    # 'gemini'  → write only to Gemini index
    # 'both'    → embed each chunk with both, write to both indexes
    embedding_provider: EmbeddingProvider = EmbeddingProvider.OPENAI

    # OpenAI (optional default so admin UI can come up first)
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-large"
    openai_embedding_dimensions: int = 3072
    embedding_batch_size: int = 64

    # Gemini (Google AI Studio API key path; Vertex AI may be added later)
    gemini_api_key: str = ""
    gemini_embedding_model: str = "gemini-embedding-2-preview"
    gemini_embedding_dimensions: int = 3072  # 128/768/1536/3072 supported
    gemini_embed_images: bool = True  # send Image elements as bytes

    # Pinecone (optional default so admin UI can come up first)
    pinecone_api_key: str = ""
    # Legacy single-index name; used as fallback for OpenAI index when
    # pinecone_index_openai is empty (back-compat with v1 deploys).
    pinecone_index: str = ""
    # Per-provider index names. Different providers can run different dims;
    # we keep them separate even when they happen to match.
    pinecone_index_openai: str = ""
    pinecone_index_gemini: str = ""
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

    # ─── MCP server ──────────────────────────────────────────────────
    # Bearer token clients must present in `Authorization: Bearer <token>`.
    # Empty disables the MCP endpoint.
    mcp_enabled: bool = True
    mcp_bearer_token: str = ""
    mcp_default_top_k: int = 10
    mcp_default_provider: str = "openai"  # which Pinecone index to search

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

    def resolved_openai_index(self) -> str:
        """Use the explicit OpenAI index name, fall back to legacy pinecone_index."""
        return self.pinecone_index_openai or self.pinecone_index

    def resolved_gemini_index(self) -> str:
        return self.pinecone_index_gemini

    def providers(self) -> list[str]:
        if self.embedding_provider == EmbeddingProvider.BOTH:
            return ["openai", "gemini"]
        return [self.embedding_provider.value]

    @property
    def graph_authority_url(self) -> str:
        """Full Microsoft Entra authority URL: '<authority>/<tenant_id>'."""
        return f"{self.graph_authority}/{self.graph_tenant_id}"

    def include_paths_list(self) -> list[str]:
        """Parsed list of path prefixes to include, normalised to lower-case."""
        out: list[str] = []
        if self.sync_path_prefix:
            out.append(self.sync_path_prefix)
        for raw in (self.sync_include_paths or "").replace(",", "\n").splitlines():
            p = raw.strip()
            if p:
                out.append(p)
        return [_normalise_path(p) for p in out]

    def exclude_paths_list(self) -> list[str]:
        out: list[str] = []
        for raw in (self.sync_exclude_paths or "").replace(",", "\n").splitlines():
            p = raw.strip()
            if p:
                out.append(p)
        return [_normalise_path(p) for p in out]

    def folder_selections(self) -> dict[str, list[str]]:
        if not self.sync_folder_selections:
            return {}
        try:
            import json
            data = json.loads(self.sync_folder_selections)
            return {
                str(k): [_normalise_path(p) for p in (v or [])]
                for k, v in data.items()
            }
        except Exception:
            return {}


def _normalise_path(p: str) -> str:
    """Lower-case a path and strip trailing slashes for prefix matching."""
    if not p:
        return ""
    s = p.strip().lower()
    while s.endswith("/"):
        s = s[:-1]
    if not s.startswith("/"):
        s = "/" + s
    return s


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
