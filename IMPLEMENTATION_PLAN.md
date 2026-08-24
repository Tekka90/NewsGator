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

## Milestone 2 — Ingestion + full-text chain ⬜

- [ ] `feedparser` fetcher with ETag / Last-Modified support
- [ ] APScheduler: per-feed adaptive polling (15–60 min), jitter
- [ ] Dedupe: `(feed_id, guid)` → canonical URL (strip `utm_*` etc.)
- [ ] Full-text chain: trafilatura direct → archive.is (`/newest/<url>`) → per-feed
      credentials → RSS excerpt + `content_status=partial` + `content_warning`
- [ ] Paywall/insufficient-text heuristics (min length, paywall markers)
- [ ] Per-domain rate limiting, robots.txt respect, archive.is probe cache (24h)
- [ ] `processing_state` transitions persisted per article
- [ ] Feed failure policy: exponential backoff (→ ~12h cap), auto-disable after
      `FEED_DISABLE_AFTER_DAYS` (default 7), manual re-enable in GUI
- [ ] Feed page shows backoff/disabled state + last error

**Acceptance**: add 3–5 real feeds (incl. one excerpt-only, one paywalled), articles
land in DB with full text where available, partial flag visible.

---

## Milestone 3 — LLM summarization + embeddings ⬜

- [ ] OpenAI-compatible client wrapper (chat + embeddings), timeouts, retries,
      JSON-mode output validation with one retry
- [ ] Prompt module: summarize (source lang → `SUMMARY_LANGUAGE`), headline, category
      (taxonomy injected from DB), novelty check, pairwise same-event check
- [ ] In-process LLM work queue (asyncio) with depth metric
- [ ] Vector store abstraction `VectorStore` protocol + `sqlite-vec` implementation
- [ ] Embed `title + summary` per article; store vector
- [ ] Language detection (`langdetect` or `fasttext` lid)
- [ ] Admin settings: LLM endpoints/models, test-connection button
- [ ] Background reprocessing job scaffold (for language/model changes; UI later)

**Acceptance**: articles get summaries in `SUMMARY_LANGUAGE`, categories from the
customizable taxonomy, embeddings stored; works against an external OpenAI-compatible
server with nothing but env config.

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

## Milestone 8 — Docker packaging + docs ⬜

- [ ] Multi-stage Dockerfile (backend + frontend build), docker-compose (app + volume)
- [ ] First-run wizard: create admin, pick `SUMMARY_LANGUAGE`, set LLM endpoints
- [ ] README: quickstart, architecture diagram, config reference
- [ ] Backup notes (SQLite file + optional Qdrant)

**Acceptance**: `docker compose up` on a fresh machine → working app.

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
