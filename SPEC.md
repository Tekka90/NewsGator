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
user, not hardcoded to English. The web GUI itself (labels, buttons, navigation) is
localized in 10 languages (en fr de es it pt ja ko zh-Hans zh-Hant), chosen per user
(`ui_language`, default: the browser language). Article embeddings are computed from the summary-language text so
clustering stays consistent regardless of source language.

**Multi-user from day one:** accounts with per-user read state and per-user settings.
Small user count (family/friends scale) — no org/team concepts.

Non-goals (v1): non-RSS sources (kept pluggable), mobile app, notifications.

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
    USER ||--o{ LISTEN_POSITION : "resumes listening"
    USER ||--o{ USER_FEED : "subscribes"
    FEED ||--o{ USER_FEED : "has subscriber"
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

    USER_FEED {
        int user_id PK, FK
        int feed_id PK, FK
        datetime created_at
    }

    FEED {
        int id PK
        string url           "RSS URL, pseudo-URL newsletter:{sender} for mail feeds, or reader:{provider}:{account_id} for reader_api feeds"
        string kind          "rss|mail|reader_api|saved — only rss feeds are polled; saved = per-user pseudo-feed (url saved:{user_id}) holding links shared from other apps"
        string sender_email  "mail feeds only: the newsletter From: address"
        string title
        int poll_interval_min
        int backfill_days   "first-poll window; NULL = server default, 0 = all"
        int email_count     "mail feeds only: newsletter emails processed"
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
        string image_url   "first RSS image: media:content → media:thumbnail → image enclosure
                            → first real <img> in the entry HTML (skips pixels/emoji and
                            lazy-load placeholder srcs like placeholder.svg — the real URL
                            lives in data-src, which feedparser's sanitizer strips);
                            else og:image recovered from the page during full-text fetch"
        text   raw_content   "from RSS (newsletter intro for mail feeds)"
        text   full_text     "fetched from source page (trafilatura)"
        string language      "ISO 639-1, detected"
        text   summary       "per-article, in SUMMARY_LANGUAGE"
        text   newsletter_intro "mail feeds only: human-written intro from the email;
                               preferred over the LLM summary when a NEW story is
                               created from this article"
        string origin_feed_title "reader_api feeds only: original subscription/feed title
                               from the third-party reader (e.g. 'Ars Technica')"
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
        string title          "LLM-generated headline, in canonical source language"
        text   summary        "merged summary, in canonical source language"
        string category
        string language       "canonical ISO language code of the story"
        string image_url      "lead image: first member article with an image
                               (RSS media / inline <img>, else og:image from the page)"
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

    STORY_TRANSLATION {
        int story_id PK,FK
        string language PK    "ISO 639-1 target code, e.g. de, fr, es"
        int version           "matches story.version when up-to-date"
        string title
        text summary
        datetime created_at
    }

    LISTEN_POSITION {
        int user_id PK,FK
        int story_id PK,FK
        string step           "summary | article | video"
        int article_id        "nullable, only for step=article (no FK)"
        int chunk
        int elapsed_seconds
        int story_version     "story.version at write time; mismatch = stale"
        int text_length       "nullable; article cleaned char_count at write time (step=article)"
        datetime updated_at   "last write wins"
    }

    ARTICLE_TRANSLATION {
        int article_id PK,FK
        string language PK    "target code = the user's summary language"
        string source_hash    "sha256 of the cleaned source text; mismatch = stale"
        text text             "translated cleaned full text (Listen)"
        datetime created_at
    }

    LLM_USAGE {
        int id PK
        datetime ts
        string kind           "summarize|embed|cluster_embed|pairwise|novelty|headline|merge|share_translate|article_translate|backfill_embed|newsletter_clean|newsletter_extract"
        string endpoint       "chat|embed"
        string model
        int prompt_tokens     "nullable — from the OpenAI `usage` object"
        int completion_tokens "nullable"
        int total_tokens      "nullable"
        int cached_tokens     "nullable, when the server reports prompt caching"
        int reasoning_tokens  "nullable, when the server reports reasoning"
        bool estimated        "true when the server omitted `usage` → chars/4 heuristic"
        int latency_ms
        int article_id        "no FK — metrics survive retention"
        int story_id          "no FK"
        int feed_id           "no FK, denormalized at insert for per-source stats"
    }

    MAIL_ACCOUNT {
        int id PK
        int user_id FK        "per-user IMAP inbox (§9 newsletter ingestion)"
        string host
        int port              "default 993"
        string username
        string password       "write-only via the API; stored in clear like other secrets"
        string folder         "mandatory — the folder receiving newsletters"
        bool   use_ssl
        bool   is_enabled
        int    last_uid       "IMAP UID watermark — only newer messages are processed"
        datetime last_checked_at
        string last_error
        datetime created_at
    }

    READER_ACCOUNT {
        int id PK
        int user_id FK        "per-user reader account (§9 third-party reader ingestion)"
        string provider       "greader (Google Reader API compatible: Inoreader, FreshRSS, Miniflux, The Old Reader, BazQux)"
        string api_base_url   "endpoint base URL (e.g. https://www.inoreader.com/reader or self-hosted)"
        string username       "API username/login or client ID"
        string password       "write-only via the API; password / token / secret"
        string auth_token     "cached session/auth token (e.g. Google Reader Auth=...)"
        bool   is_enabled
        int    virtual_feed_id "FK to feed.id: single aggregated virtual feed for this account"
        string sync_cursor    "continuation / watermark token for polling"
        datetime last_checked_at
        string last_error
        datetime created_at
    }
```

`LLM_USAGE` is append-only and **never purged by retention** — one row per external
LLM call, full history is the point (cloud-readiness metrics). Cost estimation is a
GUI-side playground (prices entered on the Usage page, never a server setting).

### Saved flag

`story_state.saved_at: datetime | None` (per user). Saved never changes `read_at_version` and never counts toward unread. Retention (`RETENTION_DAYS`) skips any story with at least one `story_state.saved_at IS NOT NULL`, along with its articles/revisions/vectors. Wording is **saved** everywhere (API, DB, UI); the Readeck `bookmark` is a separate, optional export.

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
    B --> B1[Fetch full text from source URL<br/>trafilatura; fallback to RSS content.<br/>Recovers og:image when the RSS entry had no media]
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
| `POST /auth/session-token` | issue a fresh portable token for the authenticated user — for flows that can't use cookies/headers (RSS readers on `/feed.xml`); works even when the client lost its localStorage token mid-session (valid cookie suffices) |
| `GET/PATCH /me` | profile + per-user preferences: summary language, web interface language (`ui_language`: `""` = follow the browser, else one of `en fr de es it pt ja ko zh-Hans zh-Hant`, `422` otherwise; interface chrome only — content stays in `summary_language`), story-list filter (`story_filter`, default `unread`) and ordering (`story_sort`/`story_order`) — shared across the user's devices; the payload also carries `last_preset_id` and `PATCH` accepts it (`""` clears, unknown id → `422 invalid_last_preset_id`); `story_*` stay for older clients |
| `GET /users`, `POST /users`, `PATCH /users/{id}`, `DELETE /users/{id}` | user management (admin): list, create (username/password/admin flag), reset password or toggle admin, delete. First-run `/auth/setup` only creates the initial admin; additional users come from here. Guards: the last admin can be neither demoted nor deleted, and a user cannot delete themselves; deleting a user bulk-removes their `STORY_STATE` and `LISTEN_POSITION` rows |
| `GET /stories?preset=<id>&feed_ids=&filter=all\|unread\|updated&category=&feed=&sort=updated\|published\|sources\|saved_at&order=asc\|desc&since=<ISO datetime>` | story list scoped to the requesting user's subscribed feeds with **per-user** flags; `feed` keeps only stories with at least one source article from that feed; sort by article publication date (default), last update, or source count, ascending (default: oldest first) or descending; undated stories sort by last update; `since` keeps only stories with source articles published (else fetched) at or after that instant, and with `sort=sources` ranks by that recent source count; optional `limit`/`offset` slice the result after filtering and sorting (omitted = full list) and per-story hosts/translations are only computed for the returned slice; `preset=<id>` applies a stored preset's criteria + sort + order (explicit `sort`/`order` replace the preset's; `feed`, repeatable `feed_ids`, `category`, `filter`, `saved`, `since` are ANDed on top; unknown or disabled preset → `404 {"detail": "Preset not found"}`); `sort=saved_at` orders by the user's save time (never-saved stories sort as the smallest value); `include_read=true` drops the read-status criterion (`unread` → `all`, other criteria still apply) so the deck can page back through already-read stories; `before=<story_id>`/`after=<story_id>` with `limit` return the stories immediately before/after that story in the final ordering (cursor paging, stable while stories get read; empty if the story is not in the list; 422 without `limit` or with both) |
| `GET /stories/widget-snapshot?since=<ISO datetime>&limit=12` | one lightweight call for the Apple widgets: `unread_count` plus `lists` (`latest`, `latest_unread`, `oldest`, `oldest_unread`, `most_sources`, `most_sources_unread`), each up to `limit` story items; `most_sources*` rank by sources published since `since` |
| `GET /stories/preset-counts` | `{counts: {<preset_id>: int}}` for every enabled preset with `show_count` — manual filters ignored; status-only presets share one conditional-aggregate query, each other preset costs one count query |
| `GET/PUT /me/presets`, `POST /me/presets/reset` | story presets ("Smart Views", see below): `GET` → `{presets, last_preset_id, compact}` (defaults when none stored); `PUT` replaces the whole list + `last_preset_id` + `compact` (validated, normalised, returned); `POST /reset` body `{scope: "builtin"\|"all"}`. Validation failures are `422 {"detail": {"code", "message"}}` |
| `GET /stories/counts?category=&feed=` | filter-pill counts `{all, unread, updated, saved}` for the user's visible stories (optional category/feed scope) — lets the GUI avoid fetching the full list just to count |
| `GET /stories/feed-options` | feeds subscribed by the current user that have at least one article in a story (`[{id, title, kind, url, sender_email}]`) — options for the story-list feed filter |
| `GET /stories/{id}` | story detail: merged summary + source articles + revision history (404 if user is not subscribed to any member feeds) |
| `POST /stories/import-url` | body `{url}` (http/https only, else 422): turns a link shared from another app into an article of the caller's "Shared with NewsGator" feed (`kind = 'saved'`, pseudo-URL `saved:{user_id}`, never polled, created on first use and subscribed to automatically) and enqueues it in the normal pipeline. Returns `201 {article_id, feed_id, already_known, story_id}`; a URL (tracking params stripped) already present in the user's feeds is not duplicated (`already_known: true`). **Every shared link is also flagged saved** for the sharer (`story_state.saved_at`, idempotent, read state untouched): when the pipeline first clusters the article (`cluster_article`, first clustering only, so a reprocess never undoes an unsave), when a known URL already has a story, and on manual move/merge of such an article; migration 0025 backfilled existing shared-link stories. Saved stories are never purged, so shared links no longer expire |
| `POST /stories/{id}/read` | sets `read_at_version = story.version` (per user) and pushes read state to connected third-party reader APIs for any member articles from reader feeds |
| `POST /stories/{id}/unread` | marks story unread and pushes unread state to connected third-party reader APIs for any member articles from reader feeds |
| `GET /stories/{id}/diff?from={version}` | what changed |
| `CRUD /feeds` | feed management (any authenticated user); `GET /feeds` reports feeds subscribed by the user with `story_count` / `unread_story_count` per feed (unread is per the requesting user) and `email_count` (mail feeds only). Adding an existing feed subscribes the user to it; adding a new feed registers it globally and **kicks an immediate background poll**. Deleting unsubscribes the user, and cascades deletion of the feed and orphaned articles only if no other subscribers remain |
| `POST /feeds/import-subscriptions` | multipart `file`: a Google Takeout YouTube `subscriptions.csv` (BOM/CRLF/quoted titles tolerated) or an OPML file, auto-detected → `201 {added, skipped, invalid, feeds[]}`; rows become `https://www.youtube.com/feeds/videos.xml?channel_id=UC…` feeds, polled in the background; `400` if neither format |
| `POST /feeds/import-opml` | bulk-import feeds into the user's subscriptions from an OPML file; new feeds are polled immediately in the background |
| `GET /feeds/export-opml` | export the user's subscribed RSS-kind feeds as an OPML 2.0 subscription list; mail feeds are excluded |
| `POST /feeds/{id}/refresh`, `POST /feeds/refresh` | force-poll one/all of the user's subscribed RSS feeds now, bypassing the adaptive schedule; mail feeds are rejected |
| `POST /feeds/probe` | body `{url}`: finds the RSS/Atom feed advertised by the site of a web page (`<link rel="alternate">`, common feed paths) using the discovery prober. Returns `{feed, already_followed, feeds: [{title, url, site_url, …, already_followed}]}` (`feeds` lists the feeds the page or its site declares, then up to 20 in all of the site's other feeds from Feedsearch (sections of the article's path first); `feed` is the first one, `null` when none) so clients can offer "subscribe to this feed" on stories created from shared links |
| `POST /feeds/discover` | Feed discovery, aligned with the Apple standalone pipeline. Body: `themes` (13 category names) **or** free-text `query` (exclusive; categories win), `mode` (`catalog` \| `smart`), `locale` (`fr_FR`: language filters results, country selects the news edition), `excluded_urls`. Leads come from the directory's curated topic lists (keyword search only as fallback), Google News publisher mentions (**only as a fallback**, when the directory yields fewer than 10 usable leads), and in `smart` mode LLM-suggested publications; each is verified live (parses, ≤180 days old, language match; the LLM topic check applies only to `smart`-mode suggestions). Feeds the user already follows and duplicates (normalized URL) are never returned. Each result carries `sources` (`directory`/`news`/`ai`) |
| `POST /feeds/discover/stream` | Same body and search as `/feeds/discover`, streamed as `application/x-ndjson`: `stage`, `progress` (`current`, `total`, `target`), `feed` (provisional arrival order), then `done` (`feeds`, the final ranking) or `error`; `ping` lines keep the connection alive. The search is time-bounded by `DISCOVERY_BUDGET_S`. | admin/user |
| `GET/POST /mail-accounts`, `PATCH/DELETE /mail-accounts/{id}` | per-user IMAP accounts for newsletter ingestion (any user, own accounts only — 404 across users). Folder is mandatory; the password is write-only (never returned, replace via PATCH). Deleting an account keeps the mail feeds/articles it produced |
| `POST /mail-accounts/{id}/test`, `POST /mail-accounts/{id}/poll` | probe IMAP login + folder existence (returns ok/errors, never the password), or poll the account immediately (202 + message count; **409 when a poll is already running** for that account) |
| `GET/POST /reader-accounts`, `PATCH/DELETE /reader-accounts/{id}` | per-user third-party RSS reader API accounts (Google Reader API compatible: Inoreader, FreshRSS, Miniflux, The Old Reader, BazQux; own accounts only). All articles from an account are grouped into one virtual feed. Password/token is write-only (never returned) |
| `POST /reader-accounts/{id}/test`, `POST /reader-accounts/{id}/poll` | probe reader API authentication + stream access, or poll the account immediately (202 + article count; 409 if poll currently in progress) |
| `GET/PATCH /settings` | global: retention days, freeze window, thresholds, vector backend, story summary/headline length (`summary_min_sentences`/`summary_max_sentences` 1–12, default 3/5; `headline_min_words` 0–40 where 0 = no minimum, `headline_max_words` 1–40, default 0/12; min ≤ max, else 400) (admin). Precedence: **env var > DB override > code default**; env-set keys are reported in `env_locked`, shown read-only in the GUI, and rejected by PATCH |
| `POST /settings/test-llm`, `POST /settings/test-qdrant`, `POST /settings/test-readeck` | connection probes for the external services (admin); return `ok` + errors without leaking secrets |
| `POST /stories/{id}/merge` / `POST /articles/{id}/move` | manual override when clustering is wrong (important for trust) |
| `GET /stories/articles/{article_id}/listen-text?lang=original\|summary` | Listen (read the full article aloud): `{article_id, language, text, translated, content_status}`. `text` is the article's `full_text` cleaned by `services/listen_text.py` (pure, deterministic, language-agnostic structural cleaning: HTML/entity residue, URLs, e-mails, markdown/link residue, `[n]` footnote markers, emoji, link-dense/menu-like/duplicate lines; no keyword lists; `full_text` is never modified and no cleaned text is stored). `lang=original` (default) returns it as is; `lang=summary` translates it into `user.summary_language or SUMMARY_LANGUAGE` when `article.language` is set and differs (chunked by paragraphs within the LLM context budget, `article_translate` usage kind, `translation` activity events `article_translate_start`/`article_translated`/`article_translate_error`), cached in `ARTICLE_TRANSLATION` per (article, language) and invalidated when the cleaned source hash changes or the article is reprocessed; `translated` is true only when a translation was actually applied. `404` when the article is in no story visible to the user, `409` when there is no usable full text (`content_status != "full"` — partial/paywalled is never circumvented —, cleaned text shorter than `FULLTEXT_MIN_CHARS`, or cleaned/raw ratio < 0.2), `422` for an unknown `lang` or a text needing more than `MAX_CALLS_PER_ARTICLE` translation calls, `502` on LLM failure. Story-detail `articles[]` also carry `has_full_text` (same rule as the `409`) and `char_count` (cleaned length) |
| `GET /stories/{id}/similar`, `GET /stories/articles/{id}/similar-stories` | ranked merge/move candidates for the story-detail pickers: ANN over story centroids then exact cosine re-rank, `[{id, title, similarity}]` best-first, self/current story excluded; stories/articles without vectors fall back to unscored recency order (`similarity: null`). Read-only probes — no activity events |
| `GET /stories/{id}/listen-position`, `PUT /stories/{id}/listen-position`, `DELETE /stories/{id}/listen-position` | per-user Listen resume point, synced across devices (Alembic `0029`, table `LISTEN_POSITION`, PK `(user_id, story_id)`). `GET` → `{story_id, step: "summary"\|"article"\|"video", article_id: str\|null, chunk, elapsed_seconds, story_version, text_length: int\|null, updated_at}` or `204` when none stored **or stale** (Alembic `0030`): a stored position is self-invalidating — `GET` deletes the row and returns `204` when `story_version != story.version`, or for `step=="article"` when the article is no longer a member of the story or its current cleaned `char_count` (`listen_text.full_text_info`, same as story-detail `articles[].char_count`) differs from `text_length`; evaluated lazily, nothing is purged on reprocess/version bump. `PUT` body `{step, article_id?, chunk, elapsed_seconds, story_version, text_length?}` upserts (last write wins) and returns the same shape; `article_id` (string of the integer id) is required when `step=="article"` and must belong to the story, else `422`; `chunk`/`elapsed_seconds` are integers in `0..1_000_000` (`422` otherwise); `story_version` must equal the story's current `version` (`409` otherwise — client refetches the story); `text_length` (the article's cleaned `char_count`) is required for `step=="article"` (`422`) and stored as given, stored null for other steps; `article_id` is stored null for other steps. `DELETE` → `204`, idempotent. `404` when the story is not visible to the user. No activity event is emitted: this is a high-frequency per-user UI bookmark, not a pipeline transition (invariant 6). Rows are removed with their story (retention purge, feed-deletion orphan cleanup, manual merge of the source story) and with the user; saved stories are retention-exempt so their positions persist |
| `PUT /stories/{id}/saved`, `DELETE /stories/{id}/saved` | set / clear the per-user **saved** flag (idempotent, returns `{saved, saved_at}`). Stored on `story_state.saved_at` (NULL = not saved; migration `0019`). Saved is orthogonal to read/updated and independent of Readeck. `GET /stories` accepts `saved=true` (only saved) and every story item/detail carries `saved: bool` + `saved_at`. Saved stories (and their member articles, revisions and vectors) are **exempt from retention purging and cluster aging deletion** for every user who saved them. Requires a story the user can see (404 otherwise) |
| `POST /stories/{id}/readeck` | push the story to Readeck as a permanent, self-contained bookmark (headline + summary + all source links as uploaded HTML; canonical `url` = primary source article). 404 when Readeck isn't configured; 502 on upstream failure. On success the bookmark UID is stored on `story.readeck_bookmark_id` (surfaced in list + detail so the GUI can grey out the button). Optional — enabled only when both `READECK_BASE_URL` and `READECK_TOKEN` are set (env or settings override) |
| `GET /stories/share-languages` | languages offered by the share picker (from `SHARE_LANGUAGES`, ISO codes filtered to known names) + the current summary language |
| `POST /stories/{id}/share` | build the share card (headline + merged summary + all source links; `url` = primary source article) for the client-side Web Share API / clipboard. Body `{language}` = ISO code for on-demand LLM translation of headline + summary; null/omitted = share as-is (no LLM call). Always available — nothing to configure. 400 on unsupported language or incomplete translation, 502 on LLM failure |
| `POST /chat` | chatbot (RAG over the story archive): body `{question}`; the question is embedded with `EMBED_MODEL`, matched against story centroids (ANN + exact cosine re-rank), and the top-`CHAT_TOP_K` story summaries ground the answer. Returns `{answer, stories[], latency_ms}` — each story carries `id/title/category/image_url/last_updated_at/source_hosts/similarity/cited` so the GUI renders clickable citations. 404 when `CHAT_ENABLED` is off, 502 on embed/LLM failure. Each turn (question + answer) is persisted per user so history follows them across devices |
| `GET /chat/history`, `DELETE /chat/history` | per-user chat history (server-side, cross-device): list turns oldest-first (assistant rows carry their citation cards), or wipe it (the GUI Clear button) |
| `GET /health`, `GET /stats` | ops |
| `GET /usage/summary?period=day\|month\|all`, `GET /usage/daily?days=`, `GET /usage/by-feed` | LLM token-usage metrics (admin): totals + per-kind/per-model breakdowns with throughput (tok/s), per-day series, per-source-feed history. Token counts flagged `estimated` when the server omitted `usage` |
| `GET /favicon?host=` | cached favicon proxy for source logos and feed icons (auth required; never a third-party favicon service — cache TTL via `FAVICON_CACHE_HOURS`). Fetch chain: `/favicon.ico`, then the homepage's `<link rel=icon>`, then parent domains (newsletter senders are often subdomains like `mail.example.com` with no icon of their own; never strips below two labels) |
| `GET /feed.xml?category=&unread=1&limit=` | the story archive as an RSS 2.0 feed — each item is one story (headline, merged summary, lead image as `media:content`, primary source article as link). guid is the stable `story:{id}`; `pubDate` is the original article publication date and `atom:updated` tracks the latest revision date, so a version bump marks the item updated without re-notifying it. Auth via `?token=` (RSS readers can't set headers) |
| `CRUD /categories` | taxonomy management (admin): list/create/rename/delete; deleting `Uncategorized` is rejected |
| `GET /categories/suggestions` | recurring LLM-proposed categories not yet in the taxonomy (admin) — `[{normalized_text, label, article_count, first_seen, last_seen, example_titles}]`, most-suggested first |
| `POST /categories/suggestions/accept`, `POST /categories/suggestions/dismiss` | add a proposal's label as a real category (clears its suggestion log) or suppress it from future listings (admin) |

Manual merge/split is a deliberate feature: clustering *will* be wrong sometimes, and the
user must be able to fix it. Corrections can later feed threshold tuning.

---


### Story presets ("Smart Views") — 2026-10-21, Alembic `0024`

A preset = filter + presentation. Stored per user as one JSON document on `user.story_presets`
(NULL = defaults, computed and not persisted until the first edit) with `user.last_preset_id`
and `user.presets_compact` (global icon-only bar). Reference implementation:
`services/story_presets.py`; shared test vectors `backend/tests/fixtures/story_presets_vectors.json`
(defaults, criteria matrix, sort vectors, keyword normalisation, validation codes, ordered icon catalog).

- **Model**: `{id, kind builtin|custom, label, icon, display icon+label|icon|label, enabled, position,
  sort published|updated|sources|saved_at, order asc|desc, show_count, criteria{status any|unread|updated|saved,
  feed_ids[], category_ids[] (category names), source_kinds[] (rss|mail|video|saved|reader_api),
  query, max_age_days 1–3650}}`. AND across criteria types, OR within a list; empty = no constraint.
  Built-in ids `all|unread|updated|saved` always exist once, cannot be deleted, have fixed criteria
  (editing them → `builtin_criteria_locked`); custom ids are `[A-Za-z0-9_-]{1,64}` (clients use UUIDs).
  Built-in `label: null` means "client shows its localized default name". Server sanity cap: 200 presets.
- **Defaults** (behaviour change for users with nothing stored): All = published desc; **Unread = published asc
  (oldest first)**; **Updated = sources desc (most sources first)**; Saved = saved_at desc. All enabled,
  `icon+label`, `show_count`. Icons `list`, `circle-dot`, `refresh`, `bookmark`.
- **Criteria semantics**: status as in the read-state rules (updated = read ∧ `version > read_at_version`;
  never unread); `feed_ids`: story has an article from a listed feed; `source_kinds`: story has an article from
  one of the user's feeds of that kind (**YouTube channel feeds count as `youtube`, not `rss`**); `video` matches a story with an article flagged `media_kind="video"` (see *Video articles*) in one of the user's feeds, so kinds are **not** disjoint: a shared video link is both `saved` and `video`; the former preset kind `youtube` was migrated to `video` by Alembic `0027` (the *feed* kind `youtube` for channel feeds stays);
  `max_age_days`: `story.last_updated_at >= now − N days`; `query`: keyword only (no semantic search) —
  whitespace tokens, NFKD + strip combining marks + casefold, every token a substring of
  `title + " " + summary` (stored story text, untranslated). Implemented in SQL with a connection-level
  `ng_norm()` SQLite function + `instr` (no FTS table); full scan of the user's visible stories, only when `query` is set.
- **Sort `saved_at`**: user's `story_state.saved_at`; never-saved = smallest value (last when desc, first when asc);
  ties `last_updated_at`, then id, in the same direction (same tie rule as the other keys).
- **Validation** (`PUT`): whole list; unique ids; every built-in present; ≥ 1 enabled; enums; label rules
  (custom 1–40 chars; built-in null/≤40); icon = catalog key (47 keys) or one emoji; lists ≤ 500; `query` ≤ 200
  (trimmed, empty → null); positions renumbered densely (stable on ties); unknown fields rejected. Codes:
  `invalid_payload, invalid_enum, invalid_id, invalid_label, invalid_icon, invalid_max_age, query_too_long,
  list_too_large, too_many_presets, duplicate_id, builtin_missing, builtin_criteria_locked, no_enabled_preset,
  invalid_scope`. `last_preset_id` that is unknown collapses to null.
- **Reset**: `builtin` restores the four built-ins (first, default order) and keeps customs, `compact` and a still-valid
  `last_preset_id`; `all` removes customs and clears `last_preset_id`/`compact`.
- **Visibility**: feed ids the user is not subscribed to (deleted, hidden) are dropped from presets on every read.
- No activity events (not a pipeline transition). The legacy `story_filter/story_sort/story_order` columns remain.

## 7. Web GUI (concept)

- **Main view**: story cards (headline, lead image thumbnail, merged summary,
  category chip, source favicons/logos (via the cached `/favicon` proxy)
  ×N, age, badges: `NEW` / `UPDATED`), default filter **Unread** (per-user
  overridable, persisted server-side), default-sorted by article publication date
  (oldest first — per-user overridable, persisted server-side) with read stories
  dimmed or in a separate tab. All story content is in the configured `SUMMARY_LANGUAGE`;
  GUI chrome (labels, buttons) is localized in 10 languages via `ui_language` (see §6 `/me`). The title and filter bar
  are sticky: story cards scroll behind them, and marking a story read in the
  desktop list smooth-scrolls down so the dimmed card slides out behind the
  header and the next story takes its place. On narrow screens the main
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
  decisions, errors — backed by the SSE endpoint below. A live "LLM interactions"
  panel shows each in-flight LLM call with its prompt and (once finished) its reply
  — see the observability section below. A compact "now processing"
  indicator is also visible in the main story view.
- **Usage page (admin)**: LLM token-usage dashboard — today / this month / all-time
  cards, per-day chart, per-stage / per-model / per-feed tables with tok/s throughput,
  and a client-side price playground ($ per 1M tokens, localStorage) projecting what
  the same usage would cost on a cloud provider. A warning banner shows the share of
  calls whose token counts were estimated (server returned no `usage` object).
- Filters: category, unread / updated-since-read, language of sources.
- **Story views (presets) in the web GUI** (2026-10-21): the Stories page's top bar is `PresetBar.svelte`,
  driven by `GET /me/presets` (enabled presets by `position`; per-preset `display` icon+label / icon / label, or all
  icon-only when `compact`; horizontal scroll, ←/→ focus keys, `aria-pressed`, accessible name = label + count).
  Counts come from one `GET /stories/preset-counts` call (load/refresh; Saved is updated optimistically on star/unstar).
  Selecting a view loads `GET /stories?preset=<id>`, clears every manual override and persists `last_preset_id`
  (`PATCH /auth/me`, plus `story_filter` for built-ins so older clients follow). The sort dropdown (adds *Saved date*),
  order toggle and feed/category selects are **session-only overrides** (sent as explicit `sort`/`order`/`feed`/`category`,
  never written back to the view). Settings → **Story views** (`StoryViewsSettings.svelte`): drag + ↑/↓ reorder, Shown
  toggle (last enabled view is locked), inline editor (name, icon grid + single emoji, display, sort, order, count; custom
  views also status, source types, feeds, categories, keywords, max age), add/duplicate/delete, *Reset built-ins* /
  *Reset everything* (confirm), compact-bar toggle, live bar preview, explicit *Save views* (`PUT /me/presets`).
  Pure logic is in `lib/presets.ts` and the single icon catalog (key → inline SVG, unknown → `filter`, emoji accepted)
  in `lib/presetIcons.ts`; both are unit-tested against `story_presets_vectors.json` with `npm test` (`node --test`).
- **Design system & theme centralization**: styled according to the "Editorial Gator"
  concept with all tokens centralized in `src/lib/theme.css` (primary brand Forest green
  `#146B3A`, Forest 900 `#0B3D2A`, warm Paper `#F8F7F1` with dark forest night palette
  for dark mode, Leaf green `#49A85A`, and Gator Gold `#F2B827` freshness/unread signal dot).
  Cards use 19px corner radii with subtle line borders; filters render as soft segmented
  pills. All component styling routes through semantic tokens so future redesigns require
  only CSS token changes. Existing logo assets are permanent invariants.

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
  "3 articles waiting for LLM". The depth counts queued articles **plus the one
  currently being processed** (once the worker dequeues an article, the raw queue
  size would read 0 even though the LLM is busy).
- **LLM interaction trace**: every external LLM call (chat + embeddings) is captured
  in an in-memory ring (last 100 calls; deliberately NOT persisted — prompts carry
  full article text and would bloat ACTIVITY_LOG). Each record carries the call kind
  (summarize/embed/pairwise/novelty/headline/merge/newsletter_clean/newsletter_extract/chat_*/
  share_translate/probe/other), a human label + article id, the truncated system/user
  prompts (or embedding input stats), then the raw reply, latency, token usage and
  attempt count. Records are broadcast live over the activity stream as
  `llm_interaction` payloads (status running → done|error) and snapshotted via
  `GET /activity/llm` for the Activity page's initial load. Call sites annotate the
  current call through a task-local context (no signature changes to the LLM client).
  Config: `LLM_TRACE_ENABLED` (default on), `LLM_TRACE_MAX_CHARS` (per-field clamp).
- Also exposed: `GET /activity/recent` for the page's initial load.

---

## 8. LLM integration details

- Config: `LLM_BASE_URL`, `LLM_MODEL`, `EMBED_MODEL`, `LLM_API_KEY` (dummy ok),
  `SUMMARY_LANGUAGE` (e.g. `en`, `fr`, `de`) — all LLM prompts are generated with the
  target language injected ("Summarize the following article in French…").
- Calls needed: `chat.completions` (summarize, headline, novelty check, pairwise confirm,
  category) and `embeddings`.
- **Context budget**: `LLM_CONTEXT_TOKENS` (default 32768, setting `llm_context_tokens`, 2048–4 000 000)
  is the model's usable window. Article text is never truncated: when `(window − 2000 reserved
  output − fixed prompt tokens) × 3 chars` is exceeded, `services/condense.py` summarises
  paragraph-aligned chunks map-reduce style (≤16 LLM calls per article incl. the final one; last
  resort: even sampling across the text). Events `condense_start|done|error`; usage kind
  `condense`. Mail clean/extract, chat RAG context and story translation are budgeted too.
- **YouTube** (`services/youtube.py`, the only module that knows YouTube): channels are plain RSS
  feeds (`/feeds/videos.xml?channel_id=UC…`, no new feed kind). Fulltext for a video URL = the
  caption transcript in paragraphs (original-language track, manual before auto), fetched via
  the innertube ANDROID player (fallback: `youtube-transcript-api`). Shorts (≤60 s or
  `/shorts/<id>` 200) are dropped (`processing_state=clustered`, `content_status=skipped`);
  live/upcoming stay `fetched` for the sweep; no captions → description (`partial`); unplayable →
  RSS content (`partial`). Story/article DTOs carry optional `video_id` / `media_kind="video"`.
  `POST /feeds` and `/feeds/probe` accept channel URLs, `@handle`, `UC…` ids and video URLs
  (resolved to the RSS URL; `422` if unresolvable). Shared YouTube links are canonicalised to
  `watch?v=<id>` and get title/channel/thumbnail from oEmbed (non-fatal on failure).
- **Video articles** (`services/video_detect.py`, 2026-10, Alembic `0026`/`0027`): an article whose *main content* is a
  video carries `media_kind="video"`, `video_url`, `video_embed_url`, `video_provider` (`youtube|file|embed`),
  `video_id` (YouTube only) and `video_duration` (seconds); all nullable, additive in story list/detail and article
  DTOs. Detection reads JSON-LD `VideoObject` (page-level before nested), `og:video*`/`twitter:player*`, then in-content
  `<video>`/`<source>`/provider iframes; only http(s) URLs are kept, relative URLs resolve against the page. A video is
  main content when the page itself is a video, the extracted body is under 1500 chars, or it precedes the first real
  (≥80 chars) paragraph; a small embed in a long article is ignored. YouTube links (feed items, shares, embeds) share one
  flag. Text = page blurb + caption transcript (Shorts embedded in a page are kept); a failed transcript is `partial`;
  non-YouTube videos keep the page description. Summaries use the video prompt guidance. Backfilled from article URLs.
  Parity vectors: `backend/tests/fixtures/video_detect_vectors.json` + `video_pages/*.html`.
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
- **Category suggestions** (`services/category_suggestions.py`): the same summarize
  call may return an optional `suggested_category` when none of the taxonomy fits
  well (prompted to only do so for a genuinely uncovered recurring topic, not a loose
  fit). Suggestions are logged per-article (`CATEGORY_SUGGESTION` table, no FK — same
  append-only pattern as `LLM_USAGE`/`CHAT_MESSAGE`), **never auto-applied**. An admin
  sees recurring proposals (grouped by lowercased/punctuation-stripped text — exact-ish
  dedupe, not semantic — across `>= CATEGORY_SUGGESTION_MIN_ARTICLES` distinct articles
  within `CATEGORY_SUGGESTION_WINDOW_DAYS`) on the Settings page next to the taxonomy
  editor, and can accept (adds the category, clears the log for that cluster) or
  dismiss (suppresses it going forward via `CATEGORY_PROPOSAL_DISMISSAL`). Toggle via
  `CATEGORY_SUGGESTIONS_ENABLED`.
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
  same URL for 24h). archive.is rejects non-browser agents (429), so archive lookups (and
  only those) send a Safari User-Agent; the snapshot is reduced to its `#CONTENT` wrapper
  (archive.is header/share/toolbar stripped) before extraction and paywall checks.
- **Retention job** (nightly): purge stories/articles older than `RETENTION_DAYS`
  (default 45, GUI-configurable); cascades to revisions, read states, and vectors.
- **First-poll backfill window**: on a feed's very first poll, skip entries older
  than the feed's `backfill_days` (per-feed override; NULL follows the global
  `FEED_BACKFILL_DAYS`, default 7; 0 = import everything). Undated entries are
  always kept. The window applies only to the first poll — later polls rely on
  `(feed_id, guid)` / canonical-URL dedupe, so old entries are never re-filtered.
  Skipped entries emit a `backfill_skipped` activity event.

### Newsletter ingestion (mail)

Newsletters arrive by email, not RSS. Any user can register an IMAP account +
**mandatory folder** in the Settings GUI (per-user, not admin — own accounts only);
the scheduler polls every enabled account every `MAIL_POLL_MINUTES` (default 15).

- Polling is read-only: `SELECT folder, readonly` + `BODY.PEEK[]` — messages are
  never flagged `\Seen`. Every IMAP connection uses a 30s socket timeout (a
  stalled server must never wedge a request or the scheduler). Progress is
  tracked by an IMAP **UID watermark**
  (`last_uid`, persisted per processed message — crash-safe); at most
  `MAIL_MAX_MESSAGES_PER_POLL` (default 20) new messages per poll, oldest first.
  On an account's **first sync**, messages older than `FEED_BACKFILL_DAYS` are
  skipped (but still advance the watermark).
- **Poll-now is split in two phases**: the `POST /api/mail-accounts/{id}/poll`
  endpoint runs only the fast SEARCH synchronously and answers 202 with the
  message count; body downloads + full processing run in a background task
  (failures land on `mail_fetch_error` / `mail_process_error` events and
  `account.last_error`).
- **One poll at a time per account**: the endpoint holds a per-account lock
  from the search phase until background processing finishes (released in a
  `finally`); a concurrent poll-now gets **409 Conflict** and the scheduler
  sweep silently skips the account for that round. Without it a second poll
  would reprocess the same messages — the UID watermark only advances as
  messages complete.
- **Senders become feeds**: each distinct From: address maps to a `FEED` with
  `kind = 'mail'`, `sender_email`, title = the From display name, pseudo-URL
  `newsletter:{sender}` (keeps the unique `url` constraint). Mail feeds show up in
  the feed management page (newsletter badge + sender-domain favicon via the
  `/favicon` proxy) but are never RSS-polled or force-refreshed.
- **Link extraction is code-first, LLM-refined in two passes**: the HTML body
  (or linkified plain text) is parsed for anchors; junk links (unsubscribe,
  share intents, tracking wrappers, empty anchors) are filtered and URLs
  canonicalized. Pass 1 (`NEWSLETTER_LLM_CLEAN`, default on) shows the model
  the FULL email rendered as text with placeholder link tokens
  (`[anchor](«L42»)` — never the real URLs, which are mostly tracking bloat)
  and lets it DELETE the non-news chrome (greeting/intro, socials/footer,
  sponsor credits, links back to the newsletter's own platform/site, app
  links); surviving placeholders map back to candidate URLs, so hallucination
  is impossible by construction. Pass 2 (`NEWSLETTER_LLM_EXTRACT`, default on)
  is pure text mapping: each SURVIVING link gets a composed title + the
  human-written intro text, validated against the code-extracted URL set
  (hallucinated URLs are dropped). Fallbacks at every failure: pass 1
  off/failed = all candidates kept (no chrome filtering at all — disabling it
  means no LLM filtering happens); pass 2 off/failed = the enclosing block's
  text minus the anchor.
  (Deletion-only filtering is dramatically more reliable for small models than
  one-pass triage+title+intro over dozens of near-identical entries.)
- **Each link becomes an article** on the sender's mail feed and goes through the
  standard pipeline unchanged (full-text fetch chain → summarize → embed →
  cluster). The LLM summary still drives embeddings/clustering (§4), but the
  newsletter intro is kept on `article.newsletter_intro` (and as `raw_content`
  fallback): when the article creates a **new** story, the story summary is the
  human-written intro — editorial text is preferred over the machine summary.
  Once another source attaches with new facts, the merge flow rewrites the story
  summary in `SUMMARY_LANGUAGE` as usual. `published_at` starts as the email's
  `Date:` header (mere delivery time); the direct full-text fetch replaces it
  with the linked page's real publication date — but ONLY from explicit
  metadata (og/article meta tags, JSON-LD `datePublished`), never from guessed
  values (URL paths, copyright years — those made story dates random). In any
  doubt the email date stays. Recovery flags the `fulltext_fetch` event with
  `date_recovered`. RSS entries keep their feedparser date — it is
  authoritative.
- Activity events (component `mail`): `mail_poll_start/done/failed`,
  `newsletter_feed_created`, `newsletter_clean_start/done/error`,
  `newsletter_extract_start/done/error`, `newsletter_message_error`,
  `mail_account_created/deleted`. The clean and
  extraction LLM calls are recorded as usage kinds `newsletter_clean` /
  `newsletter_extract`.

### Third-party RSS Reader API ingestion (Google Reader API compatible)

Users can connect third-party RSS reader services (e.g. Inoreader, FreshRSS, Miniflux,
The Old Reader, BazQux) using the standard Google Reader API (`/reader/api/0`).
Accounts are configured per-user (`READER_ACCOUNT`, own accounts only — 404 across
users), and the scheduler polls enabled accounts every `READER_POLL_MINUTES` (default 15).

- **Virtual Feed Aggregation**: All articles originating from a connected reader account
  are associated with **one single virtual feed** (`FEED` with `kind = 'reader_api'`,
  title e.g. "Inoreader" or custom account title, pseudo-URL `reader:{provider}:{account_id}`).
  Sub-feeds within the third-party reader do NOT appear as individual feeds in NewsGator.
- **Canonical Publisher Links**: Entry links are resolved to the **original publisher URL**
  (from `canonical[0].href` or `alternate[0].href`, stripped of tracking parameters via
  `canonicalize_url()`). Third-party redirect/tracking URLs are never stored as article URLs.
- **Publisher Favicons**: Favicons in story cards and detail rows derive automatically
  from the canonical `article.url` host (using the cached `/api/favicon?host=` proxy),
  faithfully showing the original publisher's icon rather than the reader service's icon.
- **Origin Feed Title**: The original subscription title reported by the reader API (e.g.
  `origin.title = "Ars Technica"`) is preserved on `article.origin_feed_title`, allowing
  story detail views to show the true publication name alongside the publisher favicon.
- **Individual Article Pipeline**: Each imported entry undergoes the complete standard
  pipeline (full-text extraction via trafilatura/readability → LLM summarization →
  embedding → clustering into stories).
- **Bidirectional Read-State Synchronization**:
  - **Outbound**: When a story is marked as read in NewsGator (`POST /api/stories/{id}/read`),
    all member articles originating from connected reader accounts have their read state
    pushed upstream via the Google Reader API (`POST /reader/api/0/edit-tag` with
    `a=user/-/state/com.google/read&i={item_id}`). Marking unread pushes `r=user/-/state/com.google/read`.
  - **Inbound**: During account poll, read states of existing articles are inspected.
    If an article was read on the remote service:
    - If the story contains only that single article: the story is marked read
      (`is_read = True`, `read_at_version = story.version`).
    - If the story has multiple member articles and an article was read externally:
      the story is marked as **"Updated"** (`is_read = True`, `read_at_version = story.version - 1`),
      signifying that part of the story was read while other complementary coverage remains.
- **Activity events** (component `reader`): `reader_poll_start/done/failed`,
  `reader_read_pushed`, `reader_account_created/deleted`.


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
  (default English at first setup, per-user override); GUI chrome localized per user (`ui_language`).
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
  Three kinds: `rss` (HTTP-polled), `mail` (newsletter ingestion from per-user
  IMAP folders — senders become feeds, links become articles, §9) and `saved`
  (links the user shares from other apps; never polled).
- **Newsletters**: per-user IMAP accounts with a mandatory folder, read-only
  UID-watermark polling, code-first link extraction + one LLM refinement call per
  message; human-written intros preferred over LLM summaries for new stories.
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
