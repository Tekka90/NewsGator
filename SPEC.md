# NewsGator — Technical Specification

> Status: **DRAFT — for review & refinement before implementation**

## 1. Goal

A self-hosted news reader that does **not** show a flat list of articles. Instead, it
groups articles from different RSS feeds that talk about the **same story** into a single
"story cluster", uses a **local OpenAI-compatible LLM** to summarize the cluster, and
links back to each original article.

### Core user stories

1. Fetch RSS feeds on a schedule during the day.
2. For each new article: embed it, summarize it, categorize it (local AI, OpenAI-compatible API).
3. Group articles covering the same event into one **Story** with a merged summary + links to sources.
4. When a *new* article arrives for an *existing* story (possibly in another language):
   - attach it to the story,
   - update the merged summary if it adds new information,
   - if the user already read the story, do **not** re-surface it as unread — flag it as
     **"read, updated since"**.
5. Web GUI to browse stories (not articles), mark read/unread, drill into sources.

**Language policy:** all summaries are written in a **user-configured summary language**
(`SUMMARY_LANGUAGE`, e.g. `en`, `fr`, `de`) — one language for everything, chosen by the
user, not hardcoded to English. The GUI itself (labels, buttons, navigation) is
**English-only for v1**. Article embeddings are computed from the summary-language text so
clustering stays consistent regardless of source language.

**Multi-user from day one:** accounts with per-user read state and per-user settings.
Small user count (family/friends scale) — no org/team concepts.

Non-goals (v1): non-RSS sources (kept pluggable), mobile app, localized GUI, notifications.

---

## 2. High-level architecture

```
┌─────────────┐   poll    ┌──────────────┐   OpenAI-compatible HTTP   ┌──────────────┐
│ RSS feeds    │ ────────▶ │  Ingestion    │ ────────────────────────▶ │  Local LLM    │
└─────────────┘           │  pipeline     │  (embeddings + chat)      │  (e.g. Ollama │
                          └──────┬───────┘                           │  llama.cpp,   │
                                 │                                    │  LM Studio…)  │
                                 ▼                                    └──────────────┘
                          ┌──────────────┐
                          │  SQLite +     │
                          │  vector index │
                          └──────┬───────┘
                                 ▼
                          ┌──────────────┐   REST/JSON   ┌──────────────┐
                          │  Backend API  │ ◀───────────▶ │   Web GUI     │
                          └──────────────┘               └──────────────┘
```

### Stack (confirmed)

| Concern | Decision | Why |
|---|---|---|
| Language | Python 3.12 | best ecosystem for RSS + AI glue |
| RSS parsing | `feedparser` | battle-tested |
| Full-text extraction | `trafilatura` (fallback: `readability-lxml`) | most feeds ship excerpts only |
| Scheduling | `APScheduler` (in-process) | no external broker needed |
| Relational DB | SQLite via SQLAlchemy | single-file, zero-ops |
| Vector store | `sqlite-vec` **by default**; **Qdrant as optional backend** behind a small store abstraction | zero-ops default, scale-out option for the user's existing Qdrant |
| LLM | OpenAI-compatible client pointed at an **external** server (`LLM_BASE_URL`) | LLM serving is out of scope (user runs oMLX on LAN) |
| Backend | FastAPI | async, auto OpenAPI docs |
| Frontend | **SvelteKit** SPA talking to the REST API | user choice |
| Auth | local accounts, argon2 hashes, session cookies | small user count, keep simple |
| Migrations | Alembic | cheap insurance |
| Packaging | Docker + docker-compose (Qdrant as optional compose service) | user deployment target |

---

## 3. Data model

```mermaid
erDiagram
    USER ||--o{ STORY_STATE : "has per-story state"
    FEED ||--o{ ARTICLE : produces
    STORY ||--o{ ARTICLE : groups
    STORY ||--o{ STORY_REVISION : "history"

    USER {
        int id PK
        string username
        string password_hash  "argon2"
        bool   is_admin
        string summary_language  "per-user override, default from global config"
        datetime created_at
    }

    FEED {
        int id PK
        string url
        string title
        int poll_interval_min
        int backfill_days   "first-poll window; NULL = server default, 0 = all"
        datetime last_fetched_at
        string etag
        string last_modified
    }

    ARTICLE {
        int id PK
        int feed_id FK
        string guid          "dedupe key (feed_id, guid)"
        string url
        string title
        string image_url   "first RSS image: media:content → media:thumbnail → image enclosure"
        text   raw_content   "from RSS"
        text   full_text     "fetched from source page (trafilatura)"
        string language      "ISO 639-1, detected"
        text   summary       "per-article, in SUMMARY_LANGUAGE"
        string category
        blob   embedding     "float32 (sqlite-vec) or external id (Qdrant)"
        int    story_id FK   "nullable until clustered"
        string processing_state "fetched|fulltext|summarized|embedded|clustered"
        string content_status   "full|partial"
        string content_warning  "e.g. 'requires credentials'"
        datetime published_at
        datetime fetched_at
    }

    STORY {
        int id PK
        string title          "LLM-generated headline, in SUMMARY_LANGUAGE"
        text   summary        "merged summary, in SUMMARY_LANGUAGE"
        string category
        string image_url      "lead image: first member article with an RSS image"
        blob   centroid       "recency-weighted mean of member embeddings"
        int    version        "bumped on every content change"
        bool   is_frozen      "true after freeze window; no new auto-attachments"
        datetime first_seen_at
        datetime last_updated_at
    }

    STORY_STATE {
        int user_id PK, FK
        int story_id PK, FK
        bool   is_read
        int    read_at_version  "story.version when user read it"
        datetime read_at
    }

    STORY_REVISION {
        int story_id FK
        int version
        text summary
        datetime created_at
    }
```

### Key derived flag

`updated_since_read(user, story) = is_read AND story.version > story_state.read_at_version`

This directly implements the requirement: *"if I already read it, I do not need to see it
again... it could be flagged as (read, but updated since that)"*. The story stays visually
"read" for that user but gets a badge; no unread count inflation.

---

## 4. Processing pipeline

Per poll cycle, for each new article:

```mermaid
flowchart TD
    A[New article fetched] --> B[Dedupe by feed_id+guid<br/>and canonical URL]
    B --> B1[Fetch full text from source URL<br/>trafilatura; fallback to RSS content]
    B1 --> C[Detect language]
    C --> D{language == SUMMARY_LANGUAGE?}
    D -- no --> E[LLM: translate-and-summarize<br/>into SUMMARY_LANGUAGE]
    D -- yes --> F[LLM: summarize]
    E --> G[Embed: title + summary<br/>in SUMMARY_LANGUAGE]
    F --> G
    G --> H[ANN search: nearest NON-FROZEN story<br/>centroids, cosine similarity]
    H --> I{max similarity >= τ_attach<br/>e.g. 0.82?}
    I -- yes --> J[Attach to existing story<br/>→ §5 update flow]
    I -- no --> K{similarity in gray zone<br/>τ_gray..τ_attach?}
    K -- yes --> L[LLM pairwise check:<br/>'same event? yes/no']
    L -- yes --> J
    L -- no --> M[Create new story]
    K -- no --> M
    M --> N[LLM: generate story headline]
    J --> N
    N --> O[Emit activity event<br/>→ §7 live status]
```

Every pipeline stage transition (feed poll started/finished, full-text fetched via which
path, summary started/done, embedding done, clustering decision + similarity score, story
summary regenerated) is emitted as an **activity event** to the in-app activity log and
the SSE stream (§7).

**Why embeddings in the summary language:** summarizing to one configured language first
(default `en`, chosen at first setup) and embedding that text gives one consistent vector
space, so a French article and an English article about the same iPhone launch still land
next to each other. A multilingual
embedding model is an alternative (keeps raw-language vectors comparable), but mixing
languages per-article in one space is not acceptable — whatever the choice, **all
embeddings must be produced the same way**. Decision: summary-language embeddings
(multilingual-embedding alternative rejected). The
original-language text stays stored and linked.

**Two-threshold clustering:** pure cosine similarity is noisy; a cheap LLM yes/no
confirmation only in the gray zone keeps accuracy up without paying an LLM call per pair.

---

## 5. The "third article arrives later" flow

When article C attaches to existing story S:

1. **Attach**: set `article.story_id = S.id`; recompute `S.centroid` as the running mean
   of member embeddings.
2. **Novelty check** (cheap LLM call): *"Given the current story summary and this new
   article summary, does the article add new facts?"*
   - **No new info** → add source link, bump `last_updated_at`, **do not** bump
     `S.version`. Read users see nothing change.
   - **New info** → LLM regenerates the merged summary **and refreshes the headline**
     in the same call (new facts may shift the story's angle), bump `S.version`.
3. **Read-state consequence** (via the derived flag in §3):
   - Unread story → stays unread, appears with fresh content.
   - Read story, version bumped → badge **"updated since read"**; user can open a
     **diff/changelog view** showing what was added (store per-version summary history
     in a `STORY_REVISION` table — optional v1, recommended).
   - Read story, no new info → nothing surfaces.

### Version history (recommended)

```
STORY_REVISION(story_id FK, version, summary, created_at)
```
Enables "what changed since I read it" — a genuinely useful view and cheap to store.

### Cluster aging (freezing)

Stories **auto-freeze** `FREEZE_AFTER_HOURS` (default 72, GUI-configurable) after
`first_seen_at`. Rationale: an article about "the iPhone" arriving 3 days later is usually
a *different angle* (review, analysis, follow-up), not the same breaking news.

- Frozen stories are excluded from ANN candidate search → a near-match to a frozen story
  creates a **new** story instead of attaching.
- The LLM may mark the new story as **related** to the frozen one
  (`related_story_ids`), so the UI can cross-link without merging.
- Manual attach to a frozen story stays possible via the merge UI.

### Centroid drift

Endorsed as a feature: the centroid drifting toward the "consensus" meaning of the story
is desirable. Guardrails:

- **Recency-weighted running mean** (half-life ~24h) so the centroid evolves with the
  newest reporting instead of being anchored to the first article.
- Nightly recompute from member embeddings to eliminate accumulated float error.
- Embedding dimensionality is tied to `EMBED_MODEL`; changing the embedding model
  requires a full re-embed job (same warning flow as changing `SUMMARY_LANGUAGE`).

### Feedback loop (threshold tuning)

v1 is **data-first, no online learning**:

1. Log every clustering decision (article, candidate story, similarity, gray-zone LLM
   verdict, action taken).
2. User corrections (manual merge / split / move) are stored as **labeled pairs**:
   "same story" / "different story".
3. An admin report replays logged pairs against candidate τ values → precision/recall
   curves → *suggested* thresholds, applied only on human confirmation.
4. Later option: inject confirmed "same event" pairs as few-shot examples into the
   pairwise check prompt.

---

## 6. API surface (backend → GUI)

| Endpoint | Purpose |
|---|---|
| `POST /auth/login`, `POST /auth/logout` | session auth; login/setup also return the signed token in the body — clients that cannot persist cookies (iOS standalone PWAs) send it as `Authorization: Bearer <token>` (or `?token=` for SSE) |
| `GET/PATCH /me` | profile + per-user preferences: summary language, story-list filter (`story_filter`, default `unread`) and ordering (`story_sort`/`story_order`) — shared across the user's devices |
| `GET /stories?filter=all\|unread\|updated&category=&sort=updated\|published\|sources&order=asc\|desc` | story list with **per-user** flags; sort by article publication date (default), last update, or source count, ascending (default: oldest first) or descending; unknown dates always last |
| `GET /stories/{id}` | story detail: merged summary + source articles + revision history |
| `POST /stories/{id}/read` | sets `read_at_version = story.version` (per user) |
| `POST /stories/{id}/unread` | |
| `GET /stories/{id}/diff?from={version}` | what changed |
| `CRUD /feeds` | feed management (admin); **creating a feed kicks an immediate background poll** (no waiting for the next scheduler tick) |
| `POST /feeds/import-opml` | bulk-import feeds from an OPML subscription export (admin); added feeds are polled immediately in the background |
| `POST /feeds/{id}/refresh`, `POST /feeds/refresh` | force-poll one/all feeds now, bypassing the adaptive schedule (admin) |
| `GET/PATCH /settings` | global: retention days, freeze window, thresholds, vector backend (admin). Precedence: **env var > DB override > code default**; env-set keys are reported in `env_locked`, shown read-only in the GUI, and rejected by PATCH |
| `POST /stories/{id}/merge` / `POST /articles/{id}/move` | manual override when clustering is wrong (important for trust) |
| `POST /stories/{id}/readeck` | push the story to Readeck as a permanent, self-contained bookmark (headline + summary + all source links as uploaded HTML; canonical `url` = primary source article). 404 when Readeck isn't configured; 502 on upstream failure. Optional — enabled only when both `READECK_BASE_URL` and `READECK_TOKEN` are set (env or settings override) |
| `GET /health`, `GET /stats` | ops |
| `GET /favicon?host=` | cached favicon proxy for source logos in story cards (auth required; never a third-party favicon service — cache TTL via `FAVICON_CACHE_HOURS`) |

Manual merge/split is a deliberate feature: clustering *will* be wrong sometimes, and the
user must be able to fix it. Corrections can later feed threshold tuning.

---

## 7. Web GUI (concept)

- **Main view**: story cards (headline, lead image thumbnail, merged summary,
  category chip, source favicons/logos (via the cached `/favicon` proxy)
  ×N, age, badges: `NEW` / `UPDATED`), default filter **Unread** (per-user
  overridable, persisted server-side), default-sorted by article publication date
  (oldest first — per-user overridable, persisted server-side) with read stories
  dimmed or in a separate tab. All story content is in the configured `SUMMARY_LANGUAGE`;
  GUI chrome (labels, buttons) is English-only in v1. On narrow screens the main
  view is a card deck: swipe left marks the story read and opens the next one,
  swipe right goes back to the previous one.
- **Story view**: merged summary, "what changed" accordion (revisions), list of source
  articles with language flag + per-article summary + external link.
- **Feed management page**: add/remove feeds, poll status, last errors.
- **Settings page**: per-user summary-language selector; admin section for LLM
  endpoints/models, clustering thresholds, freeze window, retention days, vector backend
  (sqlite-vec vs Qdrant).
- **Activity page (admin)**: live stream of backend operations — feed polls, full-text
  fetches (and which fallback path was used), LLM summarization in progress, clustering
  decisions, errors — backed by the SSE endpoint below. A compact "now processing"
  indicator is also visible in the main story view.
- Filters: category, unread / updated-since-read, language of sources.

---

## 7. Live activity / observability

The GUI must show what the backend is doing ("refreshing feed XYZ", "summarizing with
LLM", …) in near real time.

- **Transport**: Server-Sent Events at `GET /activity/stream` (one-way server→client;
  simpler and more firewall/proxy-friendly than WebSockets; SvelteKit consumes it with
  `EventSource`).
- **Persistence**: an `ACTIVITY_LOG` table (capped ring buffer, e.g. last 5k events) so
  the Activity page can show recent history, not just the live tail.
- **Event shape**:

```json
{
  "ts": "2026-08-24T10:15:30Z",
  "level": "info|warn|error",
  "component": "ingest|fulltext|llm|cluster|retention",
  "action": "feed_poll_start|feed_poll_done|fulltext_fetch|summarize_start|summarize_done|embed_done|cluster_attach|cluster_new|story_update|feed_disabled|...",
  "detail": {"feed": "The Verge", "article_id": 123, "similarity": 0.87, "llm_ms": 1820}
}
```

- **LLM queue visibility**: the summarization/embedding work goes through a small
  in-process queue; the activity stream includes queue depth so the GUI can show
  "3 articles waiting for LLM".
- Also exposed: `GET /activity/recent` for the page's initial load.

---

## 8. LLM integration details

- Config: `LLM_BASE_URL`, `LLM_MODEL`, `EMBED_MODEL`, `LLM_API_KEY` (dummy ok),
  `SUMMARY_LANGUAGE` (e.g. `en`, `fr`, `de`) — all LLM prompts are generated with the
  target language injected ("Summarize the following article in French…").
- Calls needed: `chat.completions` (summarize, headline, novelty check, pairwise confirm,
  category) and `embeddings`.
- All prompts request **structured JSON output**; validate + retry once on parse failure.
- Every LLM call is idempotent-ish and stored (article.summary, story.summary), so
  pipeline steps are resumable after failure — store per-article `processing_state`
  (`fetched → summarized → embedded → clustered`). Articles are handed to the LLM
  queue only after their state commit; a periodic backlog sweep
  (`BACKLOG_SWEEP_MINUTES`, default 5) requeues anything stuck in `fulltext`
  (LLM failure, lost queue item), on top of the startup requeue.
- Categories: **user-customizable taxonomy** — a seed list (Tech, World, Science,
  Business, Sports, Culture, Politics, Health) is created at install, but admins can
  add/rename/remove categories in the GUI; the constrained-choice LLM prompt is built
  from the *current* taxonomy at call time. Renaming a category relabels existing items
  (cheap); deleting moves items to `Uncategorized`.
- **Changing `SUMMARY_LANGUAGE` after data exists** requires re-summarizing and
  re-embedding everything (embeddings are in the summary language's vector space).
  Warn the user and run it as a background reprocessing job.

### Model recommendations (baseline: Apple M1 Max, 64 GB RAM, MLX-served)

The LLM server is external to this project; these are sizing suggestions for that
hardware budget:

- **Chat / summarization:** a 32B-class instruct model at 4-bit (~18–20 GB), e.g.
  Qwen2.5/3-32B-Instruct; or Mistral-Small-3.x-24B if latency matters. Prioritize
  **multilingual quality** — it must summarize *from any source language into* the
  configured `SUMMARY_LANGUAGE`. Validate multilingual output early with real feeds.
- **Embeddings:** `bge-m3` or `multilingual-e5-large` (~1–2 GB) — strong multilingual
  retrieval, ~1024-dim vectors, fine for both sqlite-vec and Qdrant.
- Headroom: 64 GB leaves room for a much larger quantized model (even 70B-class) if
  summary quality disappoints — measure first, upgrade only if needed.
- Everything goes through `LLM_BASE_URL` / `EMBED_BASE_URL`, so swapping models is a
  config change, not a code change.

---

## 9. Ingestion specifics

- Per-feed adaptive interval (e.g. 15–60 min) based on observed posting frequency.
- **Failure policy**: on fetch error, retry with exponential backoff (starting at the
  normal poll interval, doubling up to ~12h); if a feed has produced **nothing but
  failures for 7 days** (`FEED_DISABLE_AFTER_DAYS`, GUI-configurable), auto-disable it,
  surface the disabled state + last error in the feed management page, and allow manual
  re-enable.
- Respect `ETag` / `Last-Modified` to avoid re-downloading.
- Dedupe layers: `(feed_id, guid)` → canonical URL (strip tracking params: `utm_*`, etc.)
  → near-dup via high embedding similarity (≥ 0.97) for republished identical pieces.
- **Full-text fetch is the default**: most feeds ship excerpts, so every article's source
  URL is fetched and extracted with trafilatura (readability fallback); summaries use the
  full text when available, RSS content otherwise. Per-feed disable flag for flaky sites.
- **Fetch fallback chain** (per article, stop at first success):
  1. Direct fetch of the source URL (with per-feed cookies/headers if configured).
  2. Paywall or insufficient text detected → retry via **archive.is**
     (`https://archive.is/newest/<url>`) and extract from the archived copy.
  3. archive.is has no copy / fails → per-feed credentials path (user's own subscription
     cookies) if configured.
  4. All fail → use RSS excerpt, flag article `content_status = partial`, and record
     `content_warning = "source does not provide full articles; requires credentials"` —
     surfaced in the story view so partial summaries are visibly marked.
- **Paywall detection heuristics**: trafilatura output below a minimum length,
  known paywall markers ("subscribe to continue", paywall JS containers), or HTTP
  patterns typical of metered paywalls.
- Polite crawling: per-domain rate limiting, `robots.txt` respected, conditional GETs;
  archive.is lookups are rate-limited too and failures are cached (don't re-probe the
  same URL for 24h).
- **Retention job** (nightly): purge stories/articles older than `RETENTION_DAYS`
  (default 45, GUI-configurable); cascades to revisions, read states, and vectors.
- **First-poll backfill window**: on a feed's very first poll, skip entries older
  than the feed's `backfill_days` (per-feed override; NULL follows the global
  `FEED_BACKFILL_DAYS`, default 7; 0 = import everything). Undated entries are
  always kept. The window applies only to the first poll — later polls rely on
  `(feed_id, guid)` / canonical-URL dedupe, so old entries are never re-filtered.
  Skipped entries emit a `backfill_skipped` activity event.

---

## 10. Scale assumptions (v1)

~50 feeds × ~20 articles/day ≈ 1k articles/day → embeddings trivial for SQLite,
LLM calls ≈ 2–3k/day (summarize + novelty + occasional pairwise) — fine on a local model.
If the local model is slow, batch summarization and run clustering asynchronously.

---

## 11. Key decisions (summary)

Everything above is normative; this section is just a quick-reference recap.

- **Stack**: Python 3.12 + FastAPI, SQLAlchemy/SQLite, Alembic; SvelteKit SPA frontend;
  Docker / docker-compose for deployment.
- **LLM**: external OpenAI-compatible server (`LLM_BASE_URL`) — the tool never serves
  models itself. Suggested models for an M1 Max 64 GB baseline in §8.
- **Language**: all summaries/embeddings in one configurable `SUMMARY_LANGUAGE`
  (default English at first setup, per-user override); GUI chrome English-only.
- **Clustering**: summarize-then-embed in `SUMMARY_LANGUAGE`; two-threshold cosine
  matching with gray-zone LLM confirmation; stories freeze after a configurable window
  (default 72h) and late matches spawn cross-linked new stories.
- **Full text**: fetch chain = direct → archive.is → per-feed credentials (user's own
  subscriptions) → RSS excerpt flagged `partial` with a visible "requires credentials"
  warning.
- **Multi-user**: local accounts (argon2, session cookies) with per-user read state and
  per-user summary language.
- **Categories**: seeded taxonomy, fully admin-customizable in the GUI.
- **Feeds**: adaptive polling, conditional GETs, exponential backoff on errors,
  auto-disable after 7 consecutive days of failure (configurable), manual re-enable.
- **Retention**: nightly purge, default 45 days, GUI-configurable.
- **Vector store**: sqlite-vec by default; external Qdrant (URL + API key) as a
  pluggable backend — never spun up by this project.
- **Observability**: live backend status in the GUI via SSE (`/activity/stream`),
  persisted activity log, LLM queue-depth indicator (§7).
- **Feedback loop**: decision logging + user corrections + offline threshold report;
  no online learning in v1. No notifications in v1.

**Suggested build order:** (1) skeleton + auth + feeds CRUD, (2) ingestion + full-text
chain, (3) LLM summarization/embedding, (4) clustering + story versioning, (5) GUI story
views, (6) activity stream, (7) retention + settings polish, (8) Docker packaging.
