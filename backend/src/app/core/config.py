"""Application settings — everything configurable lives here (env-driven).

Invariant (SPEC.md): thresholds, windows, intervals, retention — never hardcoded.
Values here are *defaults*; several can be overridden per-install via the SETTINGS
table and the admin GUI (see app.services.settings).
"""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    # Compose passes ${VAR} through even when unset, yielding empty strings —
    # treat those as "not set" so defaults apply (and they aren't env-locked).
    @field_validator("qdrant_url", "qdrant_api_key", "embed_base_url", mode="before")
    @classmethod
    def _empty_str_is_none(cls, v: object) -> object:
        return None if v == "" else v

    @field_validator("vector_backend", mode="before")
    @classmethod
    def _empty_vector_backend_is_default(cls, v: object) -> object:
        return "sqlite_vec" if v == "" else v

    # Core
    database_url: str = "sqlite+aiosqlite:///./newsgator.db"
    secret_key: str = "change-me-in-production"  # session signing
    environment: str = "dev"

    # LLM (external OpenAI-compatible server — never served by this project)
    llm_base_url: str = "http://localhost:8080/v1"
    llm_model: str = "qwen2.5-32b-instruct"
    llm_api_key: str = "not-needed"
    embed_base_url: str | None = None  # defaults to llm_base_url when unset
    embed_model: str = "bge-m3"
    llm_timeout_s: float = 120.0

    # Language policy (SPEC §1): summaries/embeddings in this language; GUI English-only
    summary_language: str = "en"

    # Vector store: "sqlite_vec" (default, zero-ops) or "qdrant" (external)
    vector_backend: str = "sqlite_vec"
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None

    # Clustering (SPEC §4/§5)
    tau_attach: float = 0.82
    tau_gray: float = 0.70
    freeze_after_hours: int = 72

    # Ingestion (SPEC §9)
    retention_days: int = 45
    feed_disable_after_days: int = 7
    poll_interval_min_minutes: int = 15
    poll_interval_max_minutes: int = 60
    fulltext_min_chars: int = 400  # below this → try archive.is fallback
    archive_failure_cache_hours: int = 24
    backlog_sweep_minutes: int = 5  # requeue articles stuck in 'fulltext' state
    # First-poll backfill window (SPEC §9): on a feed's first poll, skip entries
    # older than this many days. 0 = import everything. Per-feed overridable.
    feed_backfill_days: int = 7

    # GUI: source favicon proxy cache (hours)
    favicon_cache_hours: int = 168


settings = Settings()
