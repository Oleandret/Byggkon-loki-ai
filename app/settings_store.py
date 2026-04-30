"""Runtime-editable settings stored in SQLite, layered on top of env vars.

Env vars provide bootstrap defaults; the admin UI writes overrides into the
`app_settings` table. On every `effective_settings()` call we merge:
    Settings(env-only)  ⊕  app_settings rows  →  Settings instance

The schema describes every editable field — type, group, whether it's a
secret (rendered masked), and a short description shown next to the field
in the admin UI.

Secrets at rest: we store them encrypted with Fernet (symmetric AES) using
a key derived from `ADMIN_SESSION_SECRET`. This isn't strong protection
against a host compromise (the key sits next to the data), but it stops
casual disk inspection from leaking your OpenAI key.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings, get_settings, reload_settings
from .logging_config import get_logger

log = get_logger(__name__)


# ─── Schema for the admin UI ──────────────────────────────────────────
@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    group: str
    kind: str  # 'text' | 'password' | 'number' | 'bool' | 'enum' | 'textarea'
    description: str = ""
    placeholder: str = ""
    options: tuple[str, ...] = ()  # for enum
    advanced: bool = False
    requires_restart: bool = False


FIELDS: tuple[FieldSpec, ...] = (
    # ─── Microsoft Graph ─────────────────────────────────────────────
    FieldSpec("graph_tenant_id", "Tenant ID", "Microsoft Graph", "text",
              "Directory (tenant) ID fra Entra ID app registration.",
              placeholder="00000000-0000-0000-0000-000000000000",
              requires_restart=True),
    FieldSpec("graph_client_id", "Client ID", "Microsoft Graph", "text",
              "Application (client) ID fra Entra ID.",
              placeholder="00000000-0000-0000-0000-000000000000",
              requires_restart=True),
    FieldSpec("graph_client_secret", "Client Secret", "Microsoft Graph", "password",
              "Client secret fra Entra ID. Vis kun verdien én gang ved opprettelse.",
              requires_restart=True),
    FieldSpec("graph_authority", "Authority URL", "Microsoft Graph", "text",
              "Som regel uendret.",
              advanced=True, requires_restart=True),
    FieldSpec("graph_scope", "Graph scope", "Microsoft Graph", "text",
              "Default: https://graph.microsoft.com/.default",
              advanced=True, requires_restart=True),
    FieldSpec("graph_base_url", "Graph base URL", "Microsoft Graph", "text",
              "Default: https://graph.microsoft.com/v1.0",
              advanced=True, requires_restart=True),

    # ─── Sync scope ──────────────────────────────────────────────────
    FieldSpec("sync_scope", "Synkroniseringsomfang", "Synkronisering", "enum",
              "Hvilke OneDrives og SharePoint-områder skal indekseres.",
              options=("all_users", "all_users_and_sharepoint",
                       "users_csv", "drives_csv")),
    FieldSpec("sync_users", "Brukere (CSV)", "Synkronisering", "textarea",
              "Komma-separerte UPN-er. Brukes når omfang = users_csv.",
              placeholder="alice@byggkon.no, bob@byggkon.no"),
    FieldSpec("sync_drive_ids", "Drive IDs (CSV)", "Synkronisering", "textarea",
              "Komma-separerte drive-IDs. Brukes når omfang = drives_csv."),
    FieldSpec("sync_path_prefix", "Sti-prefiks (valgfritt)", "Synkronisering", "text",
              "Hvis satt, indekserer kun filer under denne stien (per drive).",
              placeholder="/Documents/Knowledge"),
    FieldSpec("max_file_bytes", "Maks filstørrelse (bytes)", "Synkronisering", "number",
              "Filer større enn dette hoppes over. Default 50 MB."),

    # ─── Scheduler ───────────────────────────────────────────────────
    FieldSpec("sync_interval_minutes", "Intervall (minutter)", "Tidsplan", "number",
              "Hvor ofte synkronisering kjøres. Sett til 0 for å bruke cron i stedet."),
    FieldSpec("sync_cron", "Cron-uttrykk (valgfritt)", "Tidsplan", "text",
              "Standard cron-uttrykk. Brukes hvis intervall = 0.",
              placeholder="*/15 * * * *"),
    FieldSpec("sync_on_startup", "Kjør synkronisering ved oppstart", "Tidsplan", "bool",
              "Trigger en sync straks appen starter."),

    # ─── Embedding provider selector ─────────────────────────────────
    FieldSpec("embedding_provider", "Embedding-provider", "Embeddings", "enum",
              "OpenAI (kun tekst), Gemini Embedding 2 (multimodal — også bilder), eller begge parallelt.",
              options=("openai", "gemini", "both")),

    # ─── OpenAI ──────────────────────────────────────────────────────
    FieldSpec("openai_api_key", "OpenAI API-nøkkel", "OpenAI Embeddings", "password",
              "Brukes til å embedde chunks med valgt modell."),
    FieldSpec("openai_embedding_model", "Embedding-modell", "OpenAI Embeddings", "enum",
              "text-embedding-3-large er anbefalt.",
              options=("text-embedding-3-large", "text-embedding-3-small",
                       "text-embedding-ada-002")),
    FieldSpec("openai_embedding_dimensions", "Embedding-dimensjoner", "OpenAI Embeddings", "number",
              "Må matche Pinecone-indeksens dimensjon. 3072 for -3-large.",
              requires_restart=True),

    # ─── Gemini ──────────────────────────────────────────────────────
    FieldSpec("gemini_api_key", "Gemini API-nøkkel", "Gemini Embeddings", "password",
              "Hentes fra Google AI Studio (aistudio.google.com)."),
    FieldSpec("gemini_embedding_model", "Embedding-modell", "Gemini Embeddings", "enum",
              "Gemini Embedding 2 er multimodal (tekst + bilder + lyd + video).",
              options=("gemini-embedding-2-preview", "gemini-embedding-2",
                       "text-embedding-005")),
    FieldSpec("gemini_embedding_dimensions", "Embedding-dimensjoner", "Gemini Embeddings", "number",
              "128, 768, 1536 eller 3072 (Matryoshka). 3072 er full kvalitet.",
              requires_restart=True),
    FieldSpec("gemini_embed_images", "Embed bilder direkte", "Gemini Embeddings", "bool",
              "Hvis på: Image-elementer fra PDF-er sendes som bytes til Gemini. Av: kun OCR-tekst."),

    # ─── Felles batch ────────────────────────────────────────────────
    FieldSpec("embedding_batch_size", "Batch-størrelse", "Embeddings", "number",
              "Antall tekster per kall. 64 er en god default for begge providere."),

    # ─── Pinecone ────────────────────────────────────────────────────
    FieldSpec("pinecone_api_key", "Pinecone API-nøkkel", "Pinecone", "password",
              "Brukes til å skrive embeddings til indeksene.",
              requires_restart=True),
    FieldSpec("pinecone_index_openai", "Indeks for OpenAI", "Pinecone", "text",
              "Indeksnavn for OpenAI-vektorer. Må eksistere — kjør bootstrap-script.",
              requires_restart=True),
    FieldSpec("pinecone_index_gemini", "Indeks for Gemini", "Pinecone", "text",
              "Separat indeks for Gemini-vektorer (kan ha annen dim).",
              requires_restart=True),
    FieldSpec("pinecone_index", "Legacy: enkelt indeksnavn", "Pinecone", "text",
              "Beholdt for bakoverkompatibilitet. Brukes som fallback for OpenAI hvis pinecone_index_openai er blank.",
              advanced=True, requires_restart=True),
    FieldSpec("pinecone_namespace", "Namespace (valgfritt)", "Pinecone", "text",
              "Hvis blank brukes drive_id som namespace per drive (anbefalt)."),

    # ─── Unstructured ────────────────────────────────────────────────
    FieldSpec("unstructured_strategy", "Parse-strategi", "Unstructured / Chunking", "enum",
              "auto = hi_res for PDF/bilder, fast for resten.",
              options=("auto", "fast", "hi_res")),
    FieldSpec("unstructured_chunk_max_chars", "Maks tegn per chunk", "Unstructured / Chunking", "number",
              "Default 1500."),
    FieldSpec("unstructured_chunk_overlap", "Overlap (tegn)", "Unstructured / Chunking", "number",
              "Default 150."),

    # ─── Storage / runtime ───────────────────────────────────────────
    FieldSpec("state_dir", "State-katalog", "System", "text",
              "Hvor SQLite-DB-en ligger. På Railway: mount Volume her.",
              advanced=True, requires_restart=True),
    FieldSpec("tmp_dir", "Temp-katalog", "System", "text",
              "Brukes til midlertidig nedlasting av filer.",
              advanced=True),
    FieldSpec("log_level", "Logg-nivå", "System", "enum",
              options=("DEBUG", "INFO", "WARNING", "ERROR")),
    FieldSpec("process_concurrency", "Prosesseringssamtidighet", "System", "number",
              "Hvor mange filer kan prosesseres parallelt. 2-4 er typisk."),

    # ─── Admin ───────────────────────────────────────────────────────
    FieldSpec("admin_password", "Admin-passord", "Admin", "password",
              "Påkrevd for å logge inn på dette UI-et.",
              requires_restart=True),
    FieldSpec("admin_session_secret", "Session-secret", "Admin", "password",
              "Lang tilfeldig streng som signerer sesjons-cookies.",
              advanced=True, requires_restart=True),
    FieldSpec("admin_session_hours", "Sesjonsvarighet (timer)", "Admin", "number",
              "Hvor lenge en innlogging varer."),

    # ─── Branding ────────────────────────────────────────────────────
    FieldSpec("brand_name", "Produktnavn", "Branding", "text",
              "Vises i topbar og tittel."),
    FieldSpec("brand_owner", "Eier/firma", "Branding", "text",
              "Vises ved siden av produktnavnet."),

    # ─── MCP-server ──────────────────────────────────────────────────
    FieldSpec("mcp_enabled", "MCP-server aktivert", "MCP (eksterne LLM-er)", "bool",
              "Slå av for å skru av /mcp-endepunktet helt.",
              requires_restart=True),
    FieldSpec("mcp_bearer_token", "MCP bearer-token", "MCP (eksterne LLM-er)", "password",
              "Eksterne LLM-klienter må sende Authorization: Bearer <token>. Lag en lang tilfeldig streng.",
              requires_restart=True),
    FieldSpec("mcp_default_provider", "Default embedding-provider", "MCP (eksterne LLM-er)", "enum",
              "Hvilken Pinecone-indeks søker MCP i hvis klienten ikke spesifiserer.",
              options=("openai", "gemini")),
    FieldSpec("mcp_default_top_k", "Default top_k", "MCP (eksterne LLM-er)", "number",
              "Antall treff returnert per søk hvis klienten ikke spesifiserer."),

    # ─── Path-filtre ─────────────────────────────────────────────────
    FieldSpec("sync_include_paths", "Inkluder-stier", "Synkronisering", "textarea",
              "En sti per linje (eller komma-separert). Filer må starte med en av disse for å indekseres.",
              placeholder="/Documents/Knowledge\n/Projects/Active",
              advanced=True),
    FieldSpec("sync_exclude_paths", "Ekskluder-stier", "Synkronisering", "textarea",
              "Filer som starter med disse stiene hoppes over.",
              placeholder="/Personal\n/Photos",
              advanced=True),
    FieldSpec("sync_folder_selections", "Mappeutvalg per bruker (JSON)", "Synkronisering", "textarea",
              "Settes via Mapper-fanen. Manuell redigering kun for power-users.",
              advanced=True),
)


_FIELD_BY_KEY = {f.key: f for f in FIELDS}


# ─── Store ───────────────────────────────────────────────────────────
class SettingsStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS app_settings (
        key         TEXT PRIMARY KEY,
        value       TEXT,            -- JSON-encoded; null for "no override"
        is_secret   INTEGER NOT NULL DEFAULT 0,
        updated_at  REAL NOT NULL
    );
    """

    def __init__(self, state_dir: str, fernet_key_seed: str) -> None:
        os.makedirs(state_dir, exist_ok=True)
        self._path = os.path.join(state_dir, "sync_state.sqlite3")
        self._lock = threading.Lock()
        self._fernet = self._build_fernet(fernet_key_seed)
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)

    @staticmethod
    def _build_fernet(seed: str) -> Fernet:
        # Deterministic key from the session secret. Truncate/pad to 32 bytes.
        seed_bytes = (seed or "loki-default-key-please-change-me-in-production").encode()
        digest = hashlib.sha256(seed_bytes).digest()
        return Fernet(base64.urlsafe_b64encode(digest))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    # ─── Encrypt / decrypt secrets ───────────────────────────────────
    def _encrypt(self, plain: str) -> str:
        if not plain:
            return ""
        return self._fernet.encrypt(plain.encode()).decode()

    def _decrypt(self, token: str) -> str:
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            log.warning("settings.decrypt.invalid_token",
                        hint="Session secret changed; secret value lost.")
            return ""

    # ─── CRUD ────────────────────────────────────────────────────────
    def get_overrides(self, *, reveal_secrets: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        with self._connect() as conn:
            for row in conn.execute("SELECT key, value, is_secret FROM app_settings"):
                if row["value"] is None:
                    continue
                spec = _FIELD_BY_KEY.get(row["key"])
                value = json.loads(row["value"])
                if row["is_secret"]:
                    plain = self._decrypt(value) if isinstance(value, str) else ""
                    out[row["key"]] = plain if reveal_secrets else _mask(plain)
                else:
                    out[row["key"]] = value
                # Ignore unknown legacy keys — keep them out of the merged settings.
                if spec is None:
                    out.pop(row["key"], None)
        return out

    def get_raw_overrides(self) -> dict[str, Any]:
        """Plain-text overrides used to build effective Settings (secrets decrypted)."""
        return self.get_overrides(reveal_secrets=True)

    def set_overrides(self, updates: dict[str, Any]) -> list[str]:
        """Set/clear overrides. Returns list of keys that need a restart."""
        restart_keys: list[str] = []
        with self._lock, self._connect() as conn:
            for key, value in updates.items():
                spec = _FIELD_BY_KEY.get(key)
                if not spec:
                    continue
                if spec.requires_restart:
                    restart_keys.append(key)
                if value is None or value == "":
                    conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
                    continue
                is_secret = spec.kind == "password"
                stored = self._encrypt(str(value)) if is_secret else value
                conn.execute(
                    """
                    INSERT INTO app_settings (key, value, is_secret, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        is_secret = excluded.is_secret,
                        updated_at = excluded.updated_at
                    """,
                    (key, json.dumps(stored), 1 if is_secret else 0, time.time()),
                )
        return restart_keys

    def effective_settings(self) -> Settings:
        """Build a Settings instance from env then layer DB overrides on top."""
        return reload_settings(self.get_raw_overrides())


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "•" * len(value)
    return value[:3] + "•" * (len(value) - 6) + value[-3:]


def fields_for_ui() -> list[dict[str, Any]]:
    """Serialised field metadata for the admin UI to render."""
    return [
        {
            "key": f.key,
            "label": f.label,
            "group": f.group,
            "kind": f.kind,
            "description": f.description,
            "placeholder": f.placeholder,
            "options": list(f.options),
            "advanced": f.advanced,
            "requires_restart": f.requires_restart,
        }
        for f in FIELDS
    ]
