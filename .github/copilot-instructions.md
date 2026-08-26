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
   current taxonomy at call time. Settings precedence: **env var > DB override >
   code default** — env-set keys are `env_locked` in the settings API, shown
   read-only in the GUI, and immune to DB overrides.
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
- **Config keys**: every new setting in `core/config.py` must also be added to
  `docker/.env.example` (commented out, with a one-line doc) in the same change.

## Current status

**All 8 milestones done** (2026-08-24): backend (FastAPI, SQLAlchemy async, Alembic
0001–0004), full pipeline (ingest → fulltext chain → summarize → embed → cluster →
story versioning → freeze → retention), stories API with per-user read state, SSE
activity stream, SvelteKit GUI (stories/feeds/activity/settings with admin editors +
threshold report), external Qdrant backend option, Dockerfile + compose. 75 pytest
tests green, ruff + mypy + svelte-check clean. Post-release additions: OPML feed
import (`POST /api/feeds/import-opml` + Feeds-page upload), LLM key handling fixes
(GUI only persists changed fields; test-llm shows key hint), story images
(`article.image_url` from RSS media/enclosures → `story.image_url` lead image,
backfilled on attach/merge; Alembic 0005), headline refresh (the merge LLM call
also returns a new `headline` when new facts bump `story.version`), immediate
first poll (adding a feed or OPML-importing kicks `poll_feeds_background` in
`ingest.py` right away — skipped when `ENVIRONMENT=test`, like the scheduler).
PWA support (2026-08-25): web manifest + iOS meta (`apple-mobile-web-app-*`,
safe-area insets) in `frontend/src/app.html` + `static/manifest.webmanifest`;
auth works without cookies — login/setup return the signed session token in the
response body, the GUI keeps it in localStorage and sends `Authorization:
Bearer` (iOS standalone PWAs drop cookies); `current_user` in `api/deps.py`
accepts cookie → Bearer → `?token=` (for SSE, which can't set headers).
Story-list ordering defaults to article publication date, oldest first, the
default filter is **Unread**, and the user's filter/sort/order choices are
persisted server-side (`user.story_filter` / `story_sort` / `story_order`,
Alembic 0006/0007, `PATCH /auth/me`) so every device follows. On touch-first
devices the Stories page renders a swipeable card deck instead of the
list: swipe left = mark read + next, swipe right = previous. Swiping left
past the last story lands on an end-of-deck "all caught up" card with a
time-of-day-aware message picked locally (`pickDoneMessage` in
`routes/+page.svelte`) — deliberately no LLM call, so it's instant and
works offline in the PWA; swipe right (or the back button) returns to the
last story. The deck is chosen
by input capability, not width: `matchMedia('(pointer: coarse)')` — touch-first
devices (iPhone, iPad, Android) get it even on large screens, since iPadOS
reports as macOS to UA sniffing. Dark mode follows the OS via
`prefers-color-scheme`: all colors are CSS custom properties defined in
`routes/+layout.svelte` (`--bg`/`--surface`/`--text`/`--accent`/…) with a
dark override block — never hardcode hex colors in component styles;
`app.html` carries `color-scheme` + media-scoped `theme-color` metas. Source logos in card meta rows: `GET /api/stories` returns `source_hosts` (distinct article
hosts per story, ≤5) and the GUI renders them via a cached, auth-protected
favicon proxy `GET /api/favicon?host=` (`api/favicons.py`; `_fetch_favicon`
is the monkeypatch seam; `FAVICON_CACHE_HOURS`, failures cached 1h) — never a
third-party favicon service (the story detail page uses it too; `faviconUrl`
in `lib/api.ts` appends the session token since `<img>` can't send headers).
Feed deletion (`DELETE /feeds/{id}`) cascades by hand — the `Feed.articles`
relationship has no delete cascade and `session.delete(feed)` lazy-loads under
AsyncSession (fails at flush): bulk-delete vectors/`ClusterDecision`/
`OverridePair`/`Article`, purge now-empty stories (+ revisions/states/centroids),
then delete the feed (event `feed_deleted`). First-poll backfill window (Alembic
0008): `feed.backfill_days` (NULL = follow `FEED_BACKFILL_DAYS`, default 7; 0 =
everything) skips entries older than the window on the **first poll only**
(`last_fetched_at IS NULL`); undated entries are always kept, later polls rely on
dedupe. Skips emit a `backfill_skipped` event. Settable in the add-feed GUI
select; OPML imports use the server default. `FEED_BACKFILL_DAYS` is
GUI-overridable via settings. Readeck integration (2026-08-26, optional):
`POST /api/stories/{id}/readeck` pushes a story to a self-hosted Readeck
instance as a permanent bookmark — the content is a generated self-contained
HTML doc (headline + merged summary + lead image + all source links) uploaded
via multipart `POST /api/bookmarks` (fields `url`/`title`/`labels`/`created` +
`html` file), so it survives NewsGator's retention window; the bookmark's
canonical `url` is the primary source article (earliest published). Enabled
only when BOTH `READECK_BASE_URL` and `READECK_TOKEN` are set (env or settings
override; whitelisted keys, env-locked like the rest — empty-string env = unset
via the config validator). Service `services/readeck.py`: `is_enabled()`,
`render_story_html()`, `save_story()`; HTTP seam `_post_bookmark` is
module-level for monkeypatching; emits `save_start`/`save_done`/`save_failed`
activity events. GUI: "Save to Readeck" button on the story detail page and a
per-story icon in the list — both probe `GET /api/settings` (admin-only) to
hide when unconfigured; settings page has the two fields (token is a secret
input). Settings page is organized in grouped sections (LLM server / Vector
store / Readeck / Clustering / Ingestion), each external service with its own
"Test connection" button backed by a `POST /api/settings/test-{service}`
probe endpoint (test-llm, test-qdrant, test-readeck) — probes return `ok` +
errors, never leak secrets (only hints like a key suffix).

Notes on the current code:
- Backend lives in `backend/src/app/` (`api/`, `core/`, `models/`, `services/`,
  `workers/`); routers depend on `get_session` and `current_user`/`admin_user` deps.
- Services: `ingest.py`, `fulltext.py`, `activity.py` (emit + SSE broadcast + ring
  buffer), `llm_client.py` (mock `chat_json`/`embed` in tests), `prompts.py`,
  `process.py` (queue + `process_article`), `cluster.py`, `vectorstore.py`
  (+ `qdrant_store.py`), `retention.py`, `feedback.py`, `readeck.py` (optional
  Readeck push; seam `_post_bookmark`). HTTP seams `_http_get` /
  `_fetch_page` / `_extract_text` are module-level for monkeypatching in tests; the
  vector store is patched via `get_vector_store` on each importing module.
- Scheduler (`workers/scheduler.py`): 1-min feed tick, hourly freeze + activity prune,
  nightly retention (03:17), and a backlog sweep every `BACKLOG_SWEEP_MINUTES`
  (default 5) requeuing articles stuck in `fulltext`. Articles are handed to the LLM
  queue only **after** the ingest commit — the worker reads through a fresh session,
  so enqueuing pre-commit silently drops them. Lifespan starts the LLM worker +
  backlog requeue + scheduler (skipped when `ENVIRONMENT=test`).
- Lifespan migrates the schema to Alembic head at startup (`alembic upgrade head`
  via subprocess; legacy create_all DBs without `alembic_version` are stamped at
  the matching revision first), then `create_all` stays as a no-op safety net +
  category seeding. After settings overrides are applied, the vector backend is
  initialized via `init_vector_store(session)` — Qdrant collections are created
  there (regression: they used to never be created, so Qdrant silently failed);
  an unreachable Qdrant degrades to the in-memory store with a logged warning.
  The Docker entrypoint no longer runs Alembic itself.
- The venv is Python 3.14 (user machine); `requires-python` stays `>=3.12` per spec.
- Frontend: SvelteKit 5 runes + adapter-node; dev proxy `/api → :8000` in
  `frontend/vite.config.ts`, production proxy in `frontend/src/hooks.server.ts`
  (`BACKEND_URL`).
