# Copilot Instructions — NewsGator

> **Maintenance rule (mandatory):** whenever a change alters architecture, stack,
> conventions, data model, API surface, pipeline behavior, or any rule stated in this
> file, **update this file in the same change**. Stale instructions are worse than none.
> Also keep [SPEC.md](../SPEC.md) (normative spec) and
> [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md) (milestone checklist) in sync.

## What this project is

Self-hosted, multi-user news reader. RSS feeds are ingested, full-text is fetched,
articles are summarized/categorized/embedded via an **external OpenAI-compatible LLM
server**, and articles covering the same event are **clustered into Stories** with a
merged summary. The GUI shows stories, not article lists. See [SPEC.md](../SPEC.md) for
the full normative spec — **read it before non-trivial changes**.

## Stack (do not deviate without updating SPEC.md §2)

- **Backend**: Python 3.12, FastAPI, SQLAlchemy 2.0 + Alembic, SQLite (default),
  APScheduler, feedparser, trafilatura (+ readability fallback).
- **Frontend**: SvelteKit SPA (TypeScript) talking to the REST API only.
- **LLM**: external OpenAI-compatible server via `LLM_BASE_URL` / `EMBED_BASE_URL`.
  Never add model-serving code to this repo; never hardcode a provider (works with
  oMLX, Ollama, llama.cpp, LM Studio…).
- **Vectors**: `sqlite-vec` by default; external Qdrant as an alternative behind the
  `VectorStore` abstraction. Never couple pipeline code to one backend.
- **Deployment**: Docker / docker-compose. No Qdrant service in compose.

## Invariants — never break these

1. **Language**: all summaries are written in the configured `SUMMARY_LANGUAGE`
   (default English, per-user override). Never hardcode English. GUI chrome is
   English-only for now.
2. **Embeddings consistency**: embeddings are computed from `SUMMARY_LANGUAGE` text
   with the configured `EMBED_MODEL`. All embeddings must be produced the same way.
   Changing language or embedding model requires the full reprocessing job.
3. **Story versioning**: `story.version` bumps **only** on real content change
   (new facts). A source-only attachment updates `last_updated_at` but not `version`.
4. **Read state is per-user**: `STORY_STATE(user_id, story_id, read_at_version)`.
   `updated_since_read = is_read AND story.version > read_at_version`. Never surface a
   read story as unread again — badge it "updated".
5. **Configurability**: thresholds, freeze window, retention days, poll intervals,
   failure policy, categories — all live in settings/env, never hardcoded. Categories
   are a customizable taxonomy stored in the DB; the LLM prompt is built from the
   current taxonomy at call time.
6. **Activity events**: every pipeline stage transition (feed poll, full-text fetch
   path used, LLM start/done + latency, clustering decision + similarity, story update,
   feed disabled) emits a structured event to `ACTIVITY_LOG` and the SSE stream
   (`/activity/stream`). When adding a pipeline step, emit an event.
7. **Resumability**: articles carry `processing_state`
   (`fetched → fulltext → summarized → embedded → clustered`). Every LLM result is
   persisted immediately; pipeline steps must be safe to re-run after a crash.
8. **No paywall circumvention**: full-text fallback chain is direct → archive.is →
   per-feed user credentials → RSS excerpt flagged `partial` with a visible
   "requires credentials" warning. Do not add anything beyond this.
9. **Manual overrides**: merge/split/move corrections are stored as labeled pairs —
   they feed the threshold-tuning report. Never silently discard them.

## Conventions

- **Backend layout** (created in Milestone 1): `backend/app/` with `api/` (routers),
  `core/` (config, security), `models/`, `services/` (ingest, llm, cluster, activity),
  `workers/` (scheduler jobs). Business logic in services, not routers.
- **LLM calls**: go through the single client wrapper (timeouts, retries, JSON-mode
  validation with one retry). Prompts live in one prompts module; always request
  structured JSON.
- **Async**: FastAPI handlers and LLM/HTTP I/O are async; blocking work (feedparser,
  trafilatura) runs via `anyio.to_thread`.
- **Tests**: pytest + httpx AsyncClient; mock the LLM client in tests. Run `pytest`
  before declaring a milestone done.
- **Lint/typing**: ruff + mypy; keep them clean.
- **Migrations**: every schema change = one Alembic revision, committed with the code.
- **API changes**: update the endpoint table in SPEC.md §6.

## Current status

**Milestone 1 done** (2026-08-24): backend skeleton + auth + feeds/categories CRUD +
SvelteKit GUI (setup/login/feeds/settings) working.
**Milestone 2 done** (2026-08-24): ingestion + full-text chain — APScheduler 1-min tick
polling due feeds (adaptive interval via `feed.empty_polls`, failure backoff capped at
12h, auto-disable after `FEED_DISABLE_AFTER_DAYS`), ETag/304 support, two-layer dedupe
(`(feed_id, guid)` then canonical URL across feeds), full-text chain
direct → archive.is → RSS excerpt (`content_status=partial` + warning), robots.txt +
per-domain rate limiting, activity events persisted to ACTIVITY_LOG.
**Milestone 3 done** (2026-08-24): LLM layer — `services/llm_client.py` (single
wrapper: JSON-mode chat with one retry, embeddings, `test_connection`),
`services/prompts.py` (all prompts, `SUMMARY_LANGUAGE` injected, taxonomy from DB),
`services/process.py` (single-worker asyncio queue, `queue_depth()`, crash-recovery
`enqueue_backlog()`), `services/vectorstore.py` (`VectorStore` protocol +
SqliteVecStore + InMemoryVectorStore fallback), settings API
(`GET/PATCH /api/settings`, whitelisted runtime overrides + `/test-llm`). Articles
flow `fulltext → summarized → embedded`; clustering (→ `clustered`) is M4.
36 pytest tests green, ruff + mypy + svelte-check clean. Next: **Milestone 4
(clustering + story versioning)** per [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md).

Notes on the current code:
- Backend lives in `backend/src/app/` (`api/`, `core/`, `models/`, `services/`,
  `workers/`); routers depend on `get_session` and `current_user`/`admin_user` deps.
- Services: `services/ingest.py` (`poll_feed`, `is_due`, `effective_interval_min`),
  `services/fulltext.py` (`fetch_full_text`), `services/activity.py` (`emit`). HTTP
  seams `_http_get` / `_fetch_page` / `_extract_text` are module-level for
  monkeypatching in tests. M3 adds `llm_client.py` (mock `chat_json`/`embed` in
  tests), `prompts.py`, `process.py` (queue + `process_article`), `vectorstore.py`.
- Scheduler: `workers/scheduler.py`, started in lifespan (skipped when
  `ENVIRONMENT=test`); lifespan also starts the LLM worker and requeues the backlog.
- Lifespan currently uses `Base.metadata.create_all` + category seeding; Alembic
  revisions `0001_initial`, `0002_feed_empty_polls`, `0003_vec_tables` exist — wiring
  `alembic upgrade head` into startup is planned for Milestone 8 (Docker packaging).
- The venv is Python 3.14 (user machine); `requires-python` stays `>=3.12` per spec.
- Frontend: SvelteKit 5 runes (`$state`/`$derived`), no legacy slots; dev proxy
  `/api → :8000` in `frontend/vite.config.ts`.
