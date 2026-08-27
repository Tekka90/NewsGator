# IDEAS TO DEV

> Scratchpad of candidate features beyond RSS, with design reasoning.
> Nothing here is committed — promote to SPEC.md only once decided.
> Context: NewsGator is intentionally named *News*Gator, not RSSGator — the plan
> is to extend ingestion to any news source. Everything downstream of ingestion
> (fulltext → summarize → embed → cluster → story) is already source-agnostic, so
> each new source is essentially **a new ingest adapter emitting `Article` rows**.

---

## 1. Newsletters

### Approaches considered

1. **IMAP polling (recommended)** — user points NewsGator at a mailbox (or a
   dedicated folder/label). Poll via IMAP (`imapclient`, wrapped in
   `anyio.to_thread`). Map incoming mail to feeds via the `List-Id:` header
   (nearly every newsletter has one), the `From:` address, or a per-user
   folder rule. Most self-hosted-friendly: works with Gmail (app password),
   Fastmail, any standard provider.
2. **Built-in SMTP receiver** — NewsGator runs a small SMTP daemon and
   generates per-newsletter addresses like `news+abc123@yourhost`. Cleanest UX
   (unique address per subscription = trivial mapping, no IMAP auth), but
   requires a reachable port 25 / mail relay — much heavier operationally.
3. **Relay services** (Kill the Newsletter, etc.) — convert email to RSS. Zero
   code, but offloads data to a third party — against the project's ethos.

### Pipeline specifics (if IMAP)

- **Skip the fulltext stage entirely** — the HTML body *is* the full text.
  Extract via a `mailparser`-style pass (prefer `text/html`, fall back to
  `text/plain`), sanitize, run through trafilatura/readability to strip
  boilerplate.
- Many newsletters are **link digests**: consider a per-feed mode —
  "treat links in the body as articles and fetch them" vs. "the body is the
  article".
- **Dedupe by `Message-ID`.**
- `processing_state` starts with `fulltext` already done → straight to
  summarize.
- Nice extras later: extract `List-Unsubscribe` so users can unsubscribe from
  the GUI.
- Open question: is a mailbox per-user or per-feed? Leaning **per-user
  mailboxes, newsletters auto-become feeds**.

---

## 2. Reddit (/r/…)

### Facts

- Reddit still serves **native RSS** (`https://www.reddit.com/r/<sub>/.rss`,
  plus sorted variants like `/top/.rss?t=day`). **Low-volume subs work today
  with zero code** — worth documenting as a tip. Problem: high-volume subs
  drown the pipeline, and Reddit's own spam/low-effort noise lands in it.
- The **public JSON API** (same URL with `.json`, no auth, rate-limited but
  fine for polling) exposes `score`, `num_comments`, `upvote_ratio`, flair —
  exactly what's needed for interaction filtering.

### The threshold-timing problem

If you poll `/new` or `/hot`, most posts are *below* threshold right now but
will cross it later. Options:

- **Poll `/top?t=hour` or `/top?t=day` (recommended v1)**: Reddit's ranking has
  already done the filtering; combined with a `min_score` / `min_comments`
  config you get high signal with one cheap poll. Dedupe handles repeats.
- **Promote pattern**: ingest candidates immediately into a lightweight staging
  state, hand them to the LLM queue only once `score >= threshold` (re-check
  each poll). More accurate, but adds a "pending promotion" state and repeat
  fetches — probably overkill for v1.

### Config (per-feed, invariant 5 — never hardcoded)

- `min_score`, `min_comments`
- `sort` (`hot` / `top` / `new`), `time_window`
- Later: **flair filtering** (many subs use flairs as categories).

### Link posts vs self-posts

Decide per feed whether the article is:

- the **linked URL** (fetch full text, cluster with news coverage — the killer
  feature: Reddit discussion merging with real articles into one Story), or
- the **self-post text**.

---

## 3. Architectural suggestion (shared by both)

- Generalize `Feed` with a `source_type` column (`rss` | `newsletter` |
  `reddit`, default `rss` for back-compat) + a JSON `source_config` column for
  type-specific knobs (IMAP folder, `min_score`, …).
- Ingestion becomes a small **adapter registry** keyed by `source_type`; each
  adapter produces normalized entries for the existing dedupe/commit path.
  Everything downstream stays untouched.
- One Alembic revision; update SPEC.md + this file's promotion status when
  committed.

### Suggested order

1. **Reddit first** — smaller (one adapter + threshold config), and the RSS
   fallback already works meanwhile.
2. **Newsletters second** — bigger surface (auth, mail parsing, per-user
   mailboxes).
