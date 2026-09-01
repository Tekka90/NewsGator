# NewsGator — Implementation Plan

> Companion to [SPEC.md](SPEC.md). This is the working checklist; the spec is the
> normative reference. Update this file as milestones are completed.
>
> Status legend: ⬜ todo · 🔄 in progress · ✅ done

---

## Milestone 1 — Project skeleton, auth, feeds CRUD ✅ (2026-08-24)

**Backend**
- [x] Repo layout: `backend/` (FastAPI app), `frontend/` (SvelteKit), `docker/`, `docs/`
- [x] Python project with `pyproject.toml` (pip), ruff + mypy config
- [x] FastAPI app factory, settings via `pydantic-settings` (env-driven:
      `DATABASE_URL`, `LLM_BASE_URL`, `LLM_MODEL`, `EMBED_BASE_URL`, `EMBED_MODEL`,
      `SUMMARY_LANGUAGE`, `RETENTION_DAYS`, `FREEZE_AFTER_HOURS`, thresholds)
- [x] SQLAlchemy 2.0 models + Alembic init (SQLite default); tables: `USER`, `FEED`,
      `CATEGORY` (seeded taxonomy), settings table — **plus full M2+ schema**
      (ARTICLE, STORY, STORY_STATE, STORY_REVISION, ACTIVITY_LOG) declared upfront
- [x] Auth: register first user = admin; login/logout with session cookie (argon2)
- [x] Feeds CRUD endpoints + validation (fetch feed title on add)
- [x] Category CRUD endpoints (admin; `Uncategorized` delete-protected)
- [x] Tests: pytest + httpx AsyncClient against a test FastAPI app — **9 tests green,
      ruff + mypy clean**

**Frontend**
- [x] SvelteKit scaffold (TypeScript), API client wrapper, login page, session store
- [x] Feeds management page (list/add/remove, last-fetched status, error display)
- [x] Settings shell page (per-user summary language + admin category management)
- [x] First-run setup page (creates admin); svelte-check 0 errors, `vite build` green;
      E2E smoke-tested against live backend

**Acceptance**: create admin → login → add a feed → it appears in DB. `pytest` green. ✔

---

## Milestone 2 — Ingestion + full-text chain ✅ (2026-08-24)

- [x] `feedparser` fetcher with ETag / Last-Modified support (304 honored)
- [x] APScheduler: per-feed adaptive polling (doubles on empty polls up to max), 1-min tick
- [x] Dedupe: `(feed_id, guid)` → canonical URL (strip `utm_*`, fbclid, gclid…) — also
      dedupes the same URL across different feeds
- [x] Full-text chain: trafilatura direct → archive.is (`/newest/<url>`) → RSS excerpt +
      `content_status=partial` + `content_warning` (per-feed cookies used on direct fetch)
- [x] Paywall/insufficient-text heuristics (min length, marker phrases)
- [x] Per-domain rate limiting (2s), robots.txt respect (1h cache), archive.is failure
      cache (24h)
- [x] `processing_state` transitions persisted per article (`fetched → fulltext`)
- [x] Feed failure policy: exponential backoff (→ 12h cap), auto-disable after
      `FEED_DISABLE_AFTER_DAYS` (default 7), re-enable resets counters (M1 GUI)
- [x] Feed page shows backoff/disabled state + last error (M1)
- [x] Activity events for poll start/done/error, fulltext path used, feed disabled
      (persisted to ACTIVITY_LOG; SSE stream is M6)
- [x] Live-validated against BBC News RSS: 31 articles, full text extracted

**Acceptance**: add 3–5 real feeds (incl. one excerpt-only, one paywalled), articles
land in DB with full text where available, partial flag visible. ✔ (12 new tests,
21 total green, ruff + mypy clean)

---

## Milestone 3 — LLM summarization + embeddings ✅ (2026-08-24)

- [x] OpenAI-compatible client wrapper (`services/llm_client.py`): chat + embeddings,
      timeouts, retries, JSON-mode validation with one retry; `test_connection()` probe
- [x] Prompt module (`services/prompts.py`): summarize (source lang →
      `SUMMARY_LANGUAGE`, taxonomy injected), story headline, novelty check, merge
      summary, pairwise same-event — language name injected, never hardcoded
- [x] In-process LLM work queue (`services/process.py`): single asyncio worker,
      `queue_depth()` for the GUI, crash-recovery backlog sweep at startup
- [x] Vector store abstraction (`VectorStore` protocol) + `SqliteVecStore` (vec0 tables,
      Alembic rev 0003) + `InMemoryVectorStore` fallback for tests; Qdrant impl in M4
- [x] Embed `title + summary` per article into the store
- [x] Language detection (`langdetect`, via `anyio.to_thread`)
- [x] Admin settings API: `GET/PATCH /api/settings` (whitelisted runtime overrides
      persisted in SETTING table, applied live), `POST /api/settings/test-llm`
- [x] Background reprocessing: `enqueue_backlog()` scaffold (full reprocess UI in M7)
- [x] Bug found & fixed by tests: LLM summarize failure no longer advances the article
      to `embedded` — it stays retryable in `fulltext`

**Acceptance**: articles get summaries in `SUMMARY_LANGUAGE`, categories from the
customizable taxonomy, embeddings stored; works against an external OpenAI-compatible
server with nothing but env config. ✔ (15 new tests, 36 total green, ruff + mypy
clean; sqlite-vec extension verified working natively)

---

## Milestone 4 — Clustering + story versioning ⬜

- [ ] Centroid ANN search over non-frozen stories (cosine), thresholds `τ_attach`,
      `τ_gray` from settings
- [ ] Attach / gray-zone LLM confirm / create-new-story logic; clustering decision log
      (article, candidate, similarity, verdict, action)
- [ ] Story creation: LLM headline; `version=1`; `STORY_REVISION` row per version
- [ ] Novelty check on attach; version bump + merged-summary regeneration only when new
      facts; recency-weighted centroid (24h half-life); nightly centroid recompute
- [ ] Freeze job: `is_frozen` after `FREEZE_AFTER_HOURS`; related-story cross-linking
- [ ] `QdrantVectorStore` implementation (external URL + API key), selectable in settings
- [ ] Manual overrides: merge stories, move article → logged as labeled pairs for the
      feedback loop
- [ ] Stories API: list with per-user flags, detail with revisions, read/unread,
      diff endpoint

**Acceptance**: seed feeds about one real event from 2+ sources → one story, merged
summary, both sources linked; a late third article updates the story with a version bump;
read state shows "updated since read" badge data correctly per user.

---

## Milestone 5 — GUI story views ⬜

- [ ] Story list: cards (headline, merged summary, category chip, source count/logos,
      age, `NEW` / `UPDATED` badges), filters (category, unread, updated-since-read,
      source language), read stories dimmed
- [ ] Story detail: merged summary, "what changed" revision accordion, source articles
      (language flag, per-article summary, external link, `partial` warning)
- [ ] Read/unread actions; diff view ("what changed since I read it")
- [ ] Manual merge / move UI
- [ ] Category management UI (admin)

**Acceptance**: daily-driver usable — browse, read, badge on updates, drill to sources.

---

## Milestone 6 — Live activity stream ⬜

- [ ] `ACTIVITY_LOG` table (5k-event ring buffer)
- [ ] Event emission at every pipeline stage (feed poll, fulltext path used, LLM
      start/done with latency, cluster decision + similarity, story update, feed
      disabled)
- [ ] `GET /activity/stream` (SSE) + `GET /activity/recent`
- [ ] Activity page (admin): live tail + filters
- [ ] "Now processing" indicator + LLM queue depth in main view

**Acceptance**: watch a poll cycle live in the GUI, including LLM queue depth.

---

## Milestone 7 — Retention + settings polish ⬜

- [ ] Nightly retention job (`RETENTION_DAYS`, default 45; cascades to revisions, read
      states, vectors)
- [ ] All GUI-configurable settings wired: retention, freeze window, thresholds,
      feed-disable days, summary language (with reprocessing warning), vector backend
- [ ] Feedback report: replay decision log + user corrections vs candidate τ values →
      precision/recall suggestion page (admin)
- [ ] Health/stats endpoints + simple dashboard

**Acceptance**: change retention in GUI → old data purged; threshold report produces a
suggestion from accumulated corrections.

---

## Milestone 8 — Docker packaging + docs ✅ (2026-08-24)

- [x] Multi-stage Dockerfile (Node 22 frontend build → Python 3.12 slim runtime),
      single image runs backend (:8000) + adapter-node frontend (:3000, /api proxied
      via hooks.server.ts + BACKEND_URL)
- [x] docker-compose (single service + data volume; no Qdrant service — external only)
- [x] `alembic upgrade head` wired into container entrypoint (create_all remains as
      first-run safety net)
- [x] First-run wizard: `/setup` creates the admin; `SUMMARY_LANGUAGE` + LLM endpoints
      via env or admin settings GUI
- [x] README: Docker quickstart, dev quickstart, config reference, tests & checks
- [x] Backup story: single SQLite file in the `newsgator-data` volume (+ optional
      external Qdrant)

**Acceptance**: `docker compose up` on a fresh machine → working app. ✔ (compose
config validated; image build is the one step to run on the target machine)

---

## Post-release additions

- Story chatbot ✅ (2026-09-01): `POST /api/chat` RAG question-answering over the
  story archive — question embedded with `EMBED_MODEL`, ANN over story centroids +
  exact cosine re-rank, top-`CHAT_TOP_K` summaries ground the answer, LLM cites
  stories by id. `services/chat.py` (seams `_embed_query`/`_answer`), prompt
  `prompts.chat_answer`, `api/chat.py`, usage kinds `chat_embed`/`chat_answer`,
  `chat_query` activity events, config `CHAT_ENABLED`/`CHAT_TOP_K`/`CHAT_CANDIDATES`
  (whitelisted). GUI: `/chat` page + nav link + Settings "Chatbot" group.

---

## Engineering rules (apply to every milestone)

- Everything configurable lives in the settings table/env — **no hardcoded constants**
  for thresholds, windows, intervals.
- Every pipeline stage emits an activity event (§7 of SPEC).
- All LLM prompts request JSON; validate; retry once; log failures.
- Never embed mixed representations: embeddings always from `SUMMARY_LANGUAGE` text with
  the configured `EMBED_MODEL`; changing either triggers the reprocessing job.
- `story.version` bumps **only** on content change; per-user `read_at_version` drives
  the "updated since read" badge.
- Keep [SPEC.md](SPEC.md), this plan, and `.github/copilot-instructions.md` in sync when
  behavior changes.
