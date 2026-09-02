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
threshold report), external Qdrant backend option, Dockerfile + compose. 116 pytest
tests green, ruff + mypy + svelte-check clean. Post-release additions: OPML feed
import (`POST /api/feeds/import-opml` + Feeds-page upload), LLM key handling fixes
(GUI only persists changed fields; test-llm shows key hint), story images
(`article.image_url` from RSS media/enclosures, else the first real `<img>` in
the entry HTML — pixels/emoji skipped — else the page's `og:image` recovered
during the direct full-text fetch via trafilatura metadata; both always on, no
setting) → `story.image_url` lead image, backfilled on attach/merge; Alembic
0005; `image_recovered` flag in the `fulltext_fetch` event, headline refresh (the merge LLM call
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
`app.html` carries `color-scheme` + media-scoped `theme-color` metas.
Mobile layout rule: nothing may overflow the viewport horizontally (it
side-scrolls the whole PWA, nav included) — wide tables sit in an
`overflow-x: auto` wrapper (`.tablewrap` pattern: Activity, Usage, Settings
report), global inputs are `max-width: 100%; box-sizing: border-box`, flex
rows that hold inputs use `flex-wrap: wrap` + `min-width: 0`, and the mobile
nav links use `flex: 1 1 0; min-width: 0` so they shrink instead of pushing
the log-out icon off-screen. The nav log-out control is an icon-only SVG
button (the full "Log out" text crowded the iOS nav); a proper "Log out"
button also lives in a "Session" card on the Settings page. On the Stories
page the title + filter bar are
sticky (`position: sticky; top: var(--nav-h)` — the layout measures the nav
height into the `--nav-h` CSS var via a ResizeObserver, z-index 20: above
cards, below the ShareButton menu/sheet and the nav). A story-count chip sits
next to the "Stories" title inside the sticky header so it never scrolls away
(on touch devices it doubles as the deck position `n / N`, replacing the old
deckmeta counter), and marking a story read
in the desktop list smooth-scrolls down (`scrollPastStory`, desktop only) so
the dimmed card slides out behind the header and the next story lands right
under it. Source logos in card meta rows: `GET /api/stories` returns `source_hosts` (distinct article
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
activity events. GUI: "Save to Readeck" button on the story detail page, a
per-story icon in the list, and the same icon on the mobile swipe-deck card —
all probe `GET /api/settings` (admin-only) to hide when unconfigured; settings
page has the two fields (token is a secret input). On success the bookmark UID
is stored on `story.readeck_bookmark_id` (Alembic 0009, nullable) and returned
by `GET /api/stories` + `GET /api/stories/{id}`; the GUI greys the button when
set (still re-clickable to re-save). The legacy-DB stamp table in `main.py`
(`_LEGACY_STAMPS`) must get a new top entry whenever a revision adds a column.
Settings page is organized in grouped sections (LLM server / Vector
store / Readeck / Clustering / Ingestion), each external service with its own
"Test connection" button backed by a `POST /api/settings/test-{service}`
probe endpoint (test-llm, test-qdrant, test-readeck) — probes return `ok` +
errors, never leak secrets (only hints like a key suffix).
Story sharing (2026-08-27, always available — no external service): a
`ShareButton.svelte` component sits next to the Readeck button in all three
places (story detail, list row icon, swipe-deck card icon). Clicking opens a
language picker: "As is" calls `POST /api/stories/{id}/share` with no
language (never touches the LLM); any other language (from
`GET /api/stories/share-languages`, driven by the `SHARE_LANGUAGES` config —
comma-separated ISO codes filtered to `prompts.LANGUAGE_NAMES`, whitelisted
in settings + GUI "Sharing" group) translates headline + summary on demand
via `prompts.translate_story_text` / `llm_client.chat_json`. The endpoint
returns `{title, text, url, translated, latency_ms}`; the component then shows
a preview sheet with explicit **Share…** (Web Share API) / **Copy** buttons —
the share/clipboard call must happen on a fresh click because the translation
round-trip consumes the original gesture's transient user activation (both
`navigator.share` and `navigator.clipboard` require it, and both are
secure-context-only → legacy `execCommand('copy')` fallback for plain-HTTP
LAN access). The primary link is folded into the shared **text**, not the
separate `url` field: some share targets (Messages…) drop `text` when `url`
is set. Service `services/share.py`: `available_languages()`,
`render_share_text()`, `prepare_share()`; LLM seam `_translate` is
module-level for monkeypatching; emits `share` events
`prepare_start`/`prepare_done`/`prepare_failed`. The `/share-languages` GET
route is declared **before** `/{story_id}` so the literal path wins.
LLM usage metrics (2026-08-27): every external LLM call appends one row to
`llm_usage` (Alembic 0010; append-only, never purged, ids are plain ints
without FKs so history survives retention/feed deletion — `feed_id` is
denormalized at insert for per-source stats). Capture: `llm_client` extracts
the OpenAI `usage` object into a **task-local ContextVar `last_usage`**
(signatures unchanged so test monkeypatching still works); call sites pass it
to `services/usage.py::record()` (kinds: summarize/embed/cluster_embed/
pairwise/novelty/headline/merge/share_translate/backfill_embed — probes
deliberately excluded), which stores prompt/completion/total/cached/reasoning
tokens + latency; when the server omits `usage`, a chars÷4 heuristic fills in
and the row is flagged `estimated` (GUI shows a warning banner). API (admin):
`GET /api/usage/summary?period=day|month|all` (totals + by-kind/by-model with
tok/s — completion tok/s for chat, prompt tok/s for embeddings),
`GET /api/usage/daily?days=`, `GET /api/usage/by-feed`. GUI: `/usage` page
(nav link admin-only) with Today/Month/All-time cards, per-day CSS bar chart,
stage/model/feed tables, and a **client-side price playground** ($/1M tokens
in localStorage, cost computed in the page — deliberately NOT a server
setting, so providers/models can be compared live). `_LEGACY_STAMPS` got a
0010 top entry keyed on `("llm_usage", "estimated")`.
User management (2026-08-27): `/auth/setup` still only creates the initial
admin — all other accounts come from the admin-only `/api/users` router
(`api/users.py`, router-level `Depends(admin_user)`): `GET` list,
`POST` create (409 on duplicate username), `PATCH /{id}` reset password
and/or toggle admin, `DELETE /{id}`. Guards: the last admin can be neither
demoted nor deleted (400), self-delete is refused, and deletion bulk-removes
the user's `StoryState` rows by hand (no cascade). Emits `user_created` /
`user_deleted` activity events. No schema change — the `User` model already
had everything. GUI: "Users (admin)" card on the Settings page (create form,
reset password via prompt, make/revoke admin, delete) backed by
`api.users.*` in `lib/api.ts` + `ManagedUser` type.
Proximity-ranked story pickers (2026-09-01): the story detail page's merge and
per-source move dropdowns are `StoryPicker.svelte` comboboxes (type to filter
by title, ↑/↓/Enter/Esc keyboard nav, pick only *selects* — the Move/Merge
button still executes). Candidates come from two read-only endpoints —
`GET /api/stories/{id}/similar` (centroid-vs-centroid) and
`GET /api/stories/articles/{id}/similar-stories` (article embedding vs
centroids) — ANN then **exact cosine re-rank** (same pattern as cluster.py;
sqlite-vec's raw score is a `1/(1+L2)` proxy, so never display it as-is),
`[{id, title, similarity}]` best-first with self/current story excluded,
unscored recency fallback (`similarity: null`) when no vector exists, cap
`SIMILAR_LIMIT = 50`. The `VectorStore` protocol gained
`get_article_vector` (all three backends; Qdrant via a shared
`_retrieve_vector` helper). Picker candidates load lazily on first open so a
many-source story costs no requests until a picker is used; the score renders
as a rounded % chip. No schema change, no activity events (read-only probes).
Story chatbot (2026-09-01): `POST /api/chat` answers questions over the story
archive via RAG — the question is embedded with `EMBED_MODEL` (invariant 2),
matched against story centroids (ANN + exact cosine re-rank, never the raw
sqlite-vec proxy), and the top-`CHAT_TOP_K` story summaries (already in
`SUMMARY_LANGUAGE` — invariant 1) ground the answer. Service
`services/chat.py::ask` with module-level LLM seams `_embed_query`/`_answer`
for monkeypatching; prompt `prompts.chat_answer` returns
`{"answer", "story_ids"}` (JSON mode, citations by id). Returns
`{answer, stories[], latency_ms}`; each story carries
`id/title/category/image_url/last_updated_at/source_hosts/similarity/cited` so
the GUI renders clickable citation cards. Chat history is **server-side**
(Alembic 0011 `chat_message`: per-user rows, role `user|assistant|error`,
assistant rows carry citation cards denormalized into `stories_json` — no story
FKs, so history survives story retention/deletion like `llm_usage`). Each
`ask()` appends the question + answer; the GUI loads `GET /api/chat/history`
on mount and Clear → `DELETE /api/chat/history`. History follows the user
across devices (the earlier per-device localStorage copy was replaced).
Records usage kinds `chat_embed`/`chat_answer` and emits
`chat_query` activity events (start/done/failed). Config `CHAT_ENABLED`
(404 when off), `CHAT_TOP_K`, `CHAT_CANDIDATES` — all whitelisted in settings.
GUI: `/chat` page (`routes/chat/+page.svelte`, message list + citation cards +
`api.chat.ask`/`history`/`clearHistory`), a "Chat" nav link, and a "Chatbot"
group on the Settings page. The chat wrap height is `100dvh - var(--nav-h) -
margins` (NOT a flex-fill on a `min-height:100dvh` main, which breaks desktop);
the composer gets `safe-area-inset-bottom` padding, and the textarea is
`font-size: 1rem` with auto-grow (1 row → ~9rem).
Story RSS feed (2026-09-01): `GET /api/feed.xml` (`api/feed.py`) exposes the
story archive as RSS 2.0 — each item is one story (headline, merged summary,
lead image as `media:content`, primary source article = earliest published as
link). guid is the stable `story:{id}`; `pubDate` is the original article
publication date and `atom:updated` the latest revision date, so a version
bump marks the item updated without re-notifying it, and source-only attaches
change neither (invariant 3). Auth reuses `current_user`, so readers pass
`?token=`; optional `category` / `unread=1` / `limit` filters. Read-only — no
LLM, no activity events, no schema change. The Settings page has a per-user
"Story RSS feed" card (visible to all users, not just admins) that builds the
ready-to-paste URL from `window.location.origin` + the localStorage session
token, with a category dropdown (options derived from `api.stories.list()` —
the `/categories` taxonomy endpoint is admin-only) and an Unread-only toggle;
the Copy button uses `navigator.clipboard` with an `execCommand('copy')`
fallback for plain-HTTP LAN access (same pattern as ShareButton). The token is
fetched fresh from `POST /auth/session-token` on mount (falling back to
localStorage) — that endpoint issues a portable token for the authenticated
user and exists because localStorage may be empty mid-session even with a
valid cookie (observed on iOS standalone PWA, where the URL rendered with an
empty `token=`).

Notes on the current code:
- Backend lives in `backend/src/app/` (`api/`, `core/`, `models/`, `services/`,
  `workers/`); routers depend on `get_session` and `current_user`/`admin_user` deps.
- Services: `ingest.py`, `fulltext.py`, `activity.py` (emit + SSE broadcast + ring
  buffer), `llm_client.py` (mock `chat_json`/`embed` in tests), `prompts.py`,
  `process.py` (queue + `process_article`), `cluster.py`, `vectorstore.py`
  (+ `qdrant_store.py`), `retention.py`, `feedback.py`, `readeck.py` (optional
  Readeck push; seam `_post_bookmark`), `usage.py` (LLM token metrics;
  `record()` reads the `llm_client.last_usage` ContextVar). HTTP seams `_http_get` /
  `_fetch_page` / `_extract_text` are module-level for monkeypatching in tests; the
  vector store is patched via `get_vector_store` on each importing module.
- Scheduler (`workers/scheduler.py`): 1-min feed tick, hourly freeze + activity prune,
  nightly retention (03:17), and a backlog sweep every `BACKLOG_SWEEP_MINUTES`
  (default 5) requeuing articles stuck in `fulltext`. Articles are handed to the LLM
  queue only **after** the ingest commit — the worker reads through a fresh session,
  so enqueuing pre-commit silently drops them. Lifespan starts the LLM worker +
  backlog requeue + scheduler (skipped when `ENVIRONMENT=test`). `queue_depth()`
  in `process.py` counts queued articles **plus the in-flight one** (`_in_flight`)
  — otherwise the indicator drops to 0 while the LLM is actually busy.
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
- Project site (2026-08-28): `docs/index.html` is a self-contained GitHub Pages
  landing page (serve `main` /`docs`); screenshots in `docs/assets/shots/` are
  captured against a seeded demo DB, never the production instance. Regenerate:
  `backend/src/app/scripts/seed_demo.py` writes fake feeds/stories/activity/usage
  (login `admin`/`demo1234`) + SVG lead images under `backend/demo_assets/`
  (gitignored; serve via `python -m http.server 8899 --directory demo_assets`),
  run the backend with `DATABASE_URL=sqlite+aiosqlite:///<abs path>/demo.db`
  (absolute — the DB lands in the process cwd otherwise) and `ENVIRONMENT=test`.
