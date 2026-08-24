---
name: newsgator-pipeline
description: >-
  Use when modifying or extending the NewsGator processing pipeline (ingestion,
  full-text fetching, LLM summarization, embedding, clustering, story updates) or
  adding a new pipeline stage. Encodes the project's pipeline invariants so changes
  stay consistent with SPEC.md.
---

# NewsGator Pipeline Skill

The normative reference is `SPEC.md` at the repo root (§4 pipeline, §5 story update
flow, §7 activity events). Read it before changing pipeline code. This skill is the
checklist; the spec is the law.

## Pipeline stages (in order)

`fetched → fulltext → summarized → embedded → clustered`

Each article's `processing_state` tracks its position. Every stage must:

1. **Persist its output immediately** (summary, embedding, story_id) — the pipeline
   must be safe to re-run from any state after a crash. Never hold results only in
   memory across stages.
2. **Emit an activity event** (`ACTIVITY_LOG` + SSE `/activity/stream`) with
   component, action, and relevant detail (feed name, article id, similarity score,
   LLM latency in ms). Adding a stage without its event is an incomplete change.
3. **Run blocking I/O off the event loop** — feedparser/trafilatura via
   `anyio.to_thread`; HTTP/LLM calls are async.

## LLM rules

- All calls go through the single client wrapper (timeout, retry, JSON-mode
  validation with one retry). Never call the OpenAI client directly elsewhere.
- Prompts live in the prompts module only. Always request structured JSON.
- Summaries/headlines are written in `SUMMARY_LANGUAGE` — inject the language into
  the prompt, never hardcode a language.
- Embeddings are computed from `title + summary` text (summary language) with
  `EMBED_MODEL`. All embeddings identical provenance — no mixing.

## Clustering rules

- ANN search over **non-frozen** story centroids only.
- `≥ τ_attach`: attach directly. `τ_gray..τ_attach`: LLM pairwise "same event?" check.
  Below: new story. Thresholds come from settings, never constants.
- Attaching to a story: run the **novelty check** first. New facts → regenerate merged
  summary, bump `story.version`, add `STORY_REVISION`. No new facts → only
  `last_updated_at` changes. **Never bump version without a content change** (per-user
  "updated since read" badges depend on it).
- Centroid: recency-weighted mean (24h half-life), recomputed nightly.
- Manual merge/split/move → store as labeled pairs for the threshold feedback report.

## Full-text fetch chain (stop at first success)

1. Direct fetch (per-feed cookies/headers if configured)
2. archive.is (`https://archive.is/newest/<url>`) — rate-limited, 24h failure cache
3. Per-feed credentials (user's own subscriptions)
4. RSS excerpt → `content_status=partial` + `content_warning` ("requires credentials")

Do not add paywall circumvention beyond this chain.

## Vector store

Pipeline code talks to the `VectorStore` protocol only (sqlite-vec or Qdrant behind
it). Never import backend-specific vector code in pipeline modules.

## When you change any of the above

Update, in the same change: `SPEC.md`, `IMPLEMENTATION_PLAN.md` (if milestone scope
shifted), and `.github/copilot-instructions.md` (if an invariant/convention changed).
