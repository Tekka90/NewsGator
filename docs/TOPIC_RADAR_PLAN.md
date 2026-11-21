# Topic Radar — plan (mute / follow whole topics)

Status: draft for implementation. Branch `feature/topic-radar` (server), same-named branch in Newsgator-Apple.
Decisions: all surfaces, delivered together; default mute = "until the topic fades"; Follow included in v1.

## 1. Goal
Let a user hide (mute) or prioritise (follow) a *topic* — a cluster of stories on a broad subject
(shopping-event season, an election, a game launch…) — without keyword lists and without flooding the UI.
Everything is generic: embeddings + LLM labels, no hardcoded terms (rule 0).

## 2. Concepts
- **Topic**: cluster of Story centroids over a rolling window. Coarser than a Story, finer than a Category.
  Global (computed once, like stories). Has a LLM label + description in `SUMMARY_LANGUAGE`.
- **Rule** (`topic_rule`): per-user `mute` or `follow`, holding its own centroid snapshot + threshold + optional
  expiry, so it outlives the topic and re-matches future recurrences.
- **Ignored** (derived, never stored on the story): a story matching a user's mute rule and no follow rule.
  Not read, not unread, never "updated"; read state is never pushed to reader APIs. Saved stories are never ignored.
  Follow wins over mute. Default mute expiry: when the topic's rule stops matching for `TOPIC_FADE_DAYS`.

## 3. Server pipeline (`services/topics.py`)
1. Assign (after clustering, on create / version bump): ANN on `topic` collection + exact cosine re-rank;
   `>= TOPIC_MATCH_THRESHOLD` assigns, borderline band (`TOPIC_BORDERLINE_MARGIN`) gets one pairwise LLM check.
2. Birth (`topic_refresh` scheduler job, every `TOPIC_REFRESH_MINUTES`): greedy density clustering of unassigned
   story centroids in `TOPIC_WINDOW_DAYS`; keep clusters with `>= TOPIC_MIN_STORIES` stories and
   `>= TOPIC_MIN_SOURCES` distinct source hosts.
3. Label: one LLM call per new topic (≈5 headlines) → `{label, description}`; fallback = most central headline.
4. Drift/fade: centroid = running mean; topic `faded` after `TOPIC_FADE_DAYS` without a new story.
5. Rule matching: a rule matches a story when cosine(story centroid, rule centroid) `>= rule.threshold`.
6. Events: `topic_created|assigned|faded|labeled|rule_matched`; usage kinds `topic_label`, `topic_pairwise`.

## 4. Schema (one Alembic revision 0028 + `_LEGACY_STAMPS` entry)
- `topic(id, label, description, label_language, story_count, source_count, first_seen_at, last_story_at, status)`
- `story_topic(story_id PK, topic_id PK, similarity)`
- `topic_rule(id, user_id, mode, label, centroid BLOB, embed_model, threshold, source_topic_id nullable, expires_at nullable, created_at)`
- `topic_feedback(user_id, story_id, rule_id, kind, created_at)` — "not about this" labeled pairs (invariant 9).
- Vector collection `topic` via `VectorStore` (all three backends).

## 5. API (all under /api; document in SPEC §6 + APPLE_CLIENT_COMPATIBILITY.md)
- `GET /topics` — trending for the user: `[{id,label,description,story_count,source_count,unread_count,velocity,rule}]`,
  capped at `TOPIC_MAX_VISIBLE`, only topics above `TOPIC_MIN_USER_SHARE` of the user's unread.
- `GET /topics/{id}/preview` — sample headlines + `would_hide_unread`.
- `POST /topics/{id}/rules {mode, expires_at?}` / `GET /topic-rules` / `PATCH|DELETE /topic-rules/{id}`.
- `POST /stories/{id}/topic-feedback {rule_id}` — "not about this topic" (tightens that rule's threshold).
- `GET /stories`: `ignored=true|false|only`; default hides ignored; `/counts`, `/preset-counts` exclude ignored and
  expose `ignored` count; story payload gains `topic_ids`, `ignored_by` (rule id). Follow adds `followed` flag + sort boost
  never reordering past the user's chosen sort (badge + pin within same day bucket).
- SQL: ignored is a join `story_topic ⋈ topic_rule` plus centroid rules for stories not yet assigned; keep `_flag_conditions`
  and `_criteria_conditions` in sync.

## 6. Web (SvelteKit)
- `TopicStrip.svelte` (≤ `TOPIC_MAX_VISIBLE` chips sized by volume, shown only when relevant), `TopicSheet.svelte`
  (preview, Mute until-fades/7d/30d/forever, Follow), "N hidden" chip + Ignored Smart View, Settings "Topic rules" card
  (+ admin tuning group). i18n: keys in `en/topics.json` + 9 locales, `npm run i18n:check`. Colors via `theme.css` tokens.

## 7. Apple
- ServerClient: DTOs + endpoint methods; `WorkspaceBackend` gains topic methods; shared `TopicStrip`/sheet in
  NewsGatorShared; iOS bottom sheet, Mac popover. Server mode never runs local AI.
- Standalone: `TopicActor` (NewsGatorCore) — same algorithm on SwiftData, same embeddings as local clustering,
  `@Generable TopicLabel` via `ModelProvider.resolve()`; runs inside existing refresh/processing tasks.
  CloudKit syncs **rules only** (label, centroid Data, embed model id, threshold, mode, expiry); topics are derived per device.
  A rule with a different embed model id is re-derived from its label.
- Localization in all 10 languages (Shared + Mac + iOS `Localizable.xcstrings`, `scripts/check-localizations.py`),
  AppHelp Knowledge bundle + `log.md`, `swift test --filter AppHelp`.

## 8. Shared contract
`backend/tests/fixtures/topic_vectors.json`: rule-matching (threshold, follow>mute, saved exemption, expiry, fade) and
assignment cases; Python reference `services/topics.py::rule_matches` and Swift twin must both pass.

## 9. Config (`core/config.py` + `docker/.env.example` + settings whitelist)
`TOPICS_ENABLED`, `TOPIC_WINDOW_DAYS`, `TOPIC_MIN_STORIES`, `TOPIC_MIN_SOURCES`, `TOPIC_MATCH_THRESHOLD`,
`TOPIC_BORDERLINE_MARGIN`, `TOPIC_REFRESH_MINUTES`, `TOPIC_FADE_DAYS`, `TOPIC_MAX_VISIBLE`, `TOPIC_MIN_USER_SHARE`.

## 10. Order of work
1. Server: migration, models, vector collection, `topics.py` + tests (incl. vectors), scheduler, API, stories filtering.
2. Web UI + i18n. 3. Docs (SPEC, copilot-instructions, compat doc).
4. Apple ServerClient + Shared UI. 5. Standalone TopicActor + CloudKit rules. 6. Localization + AppHelp.
Each stage: backend `pytest` + `ruff` + `mypy`; `npm run check`/`i18n:check`; `swift test -warnings-as-errors` per package.

## 11. Risks
Embedding over-reach (mitigated by margin + LLM check + feedback); very broad topics (preview count makes it visible);
cross-device model mismatch (rule stores embed model id).
