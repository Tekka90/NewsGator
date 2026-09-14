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
    @field_validator(
        "qdrant_url", "qdrant_api_key", "embed_base_url", "readeck_base_url",
        "readeck_token", mode="before"
    )
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

    # Live LLM interaction trace (Activity page): in-memory only, never written
    # to the DB; broadcasts truncated prompts/replies over the activity SSE stream.
    llm_trace_enabled: bool = True
    llm_trace_max_chars: int = 8000  # per text field, longer text is clamped

    # Language policy (SPEC §1): summaries/embeddings in this language; GUI English-only
    summary_language: str = "en"

    # Sharing: languages offered when sharing a story (comma-separated ISO 639-1
    # codes, filtered to the known list in prompts.LANGUAGE_NAMES). Translation
    # is done on demand by the LLM; "as is" sharing never calls the LLM.
    share_languages: str = "en,fr,de,es,it,pt,nl"

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

    # Newsletter ingestion (SPEC §9): per-user IMAP accounts polled for
    # newsletters; each sender becomes a mail feed, each extracted link an article.
    mail_poll_minutes: int = 15  # how often the scheduler polls all accounts
    mail_max_messages_per_poll: int = 20  # cap per account per poll (oldest first)
    # One LLM call per newsletter maps each link to its human-written intro;
    # when off, a code heuristic (enclosing block text) is used instead.
    newsletter_llm_extract: bool = True
    # One LLM call per newsletter FIRST deletes the non-news chrome (intro,
    # socials/footer, sponsor credits, platform self-links) from the full email
    # rendered with placeholder link tokens; the extract call then only sees the
    # surviving news links. Far more reliable than one-pass triage.
    newsletter_llm_clean: bool = True

    # GUI: source favicon proxy cache (hours)
    favicon_cache_hours: int = 168

    # Chatbot (RAG over story centroids + summaries — SPEC §10)
    chat_enabled: bool = True
    chat_top_k: int = 12  # stories handed to the LLM as grounding
    chat_candidates: int = 40  # ANN candidates re-ranked by exact cosine

    # Readeck integration (optional — enabled only when both are set)
    readeck_base_url: str | None = None
    readeck_token: str | None = None

    # Category-suggestion detection (services/category_suggestions.py): the
    # summarize LLM call may propose a new category when none of the taxonomy
    # fits; recurring proposals surface to admins on the Settings page.
    category_suggestions_enabled: bool = True
    category_suggestion_min_articles: int = 5  # distinct articles before proposing
    category_suggestion_window_days: int = 30  # rolling window for the count


settings = Settings()
