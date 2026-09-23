# Apple client (`Newsgator-Apple`) server compatibility

## Current integration contract

The native app adapts to this server's existing application API. The server
remains authoritative; no compatibility aliases or raw LLM bridge are required.
All API routers below are mounted under **`/api`**, including feeds, usage,
activity, authentication, settings, and chat.

| Native capability | Server contract |
| --- | --- |
| Login and first-run setup | `/api/auth/login`, `/api/auth/setup-needed`, `/api/auth/setup`; issued tokens belong in Keychain. |
| Story browsing | `/api/stories` returns an array with integer IDs, one category, versions and per-user read state; there is no pagination envelope. |
| Reading and editorial actions | Story detail, read/unread, diff, similar/merge, article move/reprocess, share/translation and Readeck endpoints. |
| In-app source browsing | Native apps embed publisher pages directly via `WKWebView` tabs; the Web app opens source links externally in new browser tabs due to publisher `X-Frame-Options` / CSP iframe restrictions. |
| Non-admin browsing | `/api/stories/feed-options`; do not require admin-only `/api/feeds` or usage endpoints to synchronize a reader's library. |
| Chat | `/api/chat` and `/api/chat/history`; archive retrieval and history stay on the server. Responses are JSON, not a token stream. |
| Feed management | `/api/feeds` CRUD, global/per-feed refresh and multipart OPML import/export; administrative permissions apply. |
| Taxonomy | `/api/categories` and category suggestions; do not replace this with native-generated free-form tags. |
| Newsletters | Per-user `/api/mail-accounts`; `kind=mail` feeds are not RSS endpoints. |
| Reader accounts | Per-user `/api/reader-accounts`; `kind=reader_api` virtual feeds aggregate all remote subscriptions with bidirectional read-state sync. |
| Activity | `/api/activity/recent`, `/pipeline`, `/llm`, `/stream`; SSE data uses `action`, `component`, arbitrary JSON `detail`, and `ts`. |
| Administration | `/api/settings`, service probes, threshold report, `/api/users`, `/api/usage/*`. Costs are client-side estimates, not server measurements. |

Native URLSession attaches bearer headers to REST and SSE requests. Query-string
tokens are needed only when explicitly generating a portable RSS subscription
URL for a reader that cannot attach headers. Native server mode never calls
`/api/llm` or invokes Apple/device-side models. An unavailable chatbot is an
explicit server configuration error, not permission to fall back to another LLM.

The native implementation plan and delivery report live in
`../Newsgator-Apple/docs/SERVER-PARITY-PLAN.md` and
`../Newsgator-Apple/docs/SERVER-PARITY-REPORT.md`.

## Historical design notes (superseded)

The analysis below records the original mismatch. Its suggestions to add
`/api/llm`, its unprefixed route examples, and its statements about missing
native screens are historical, not the current implementation plan.

> Analysis + TODO for making this server (the real, running implementation)
> compatible with the `NewsGatorServerClient` Swift package in the sibling
> `../Newsgator-Apple` repo. That package was written against an **assumed**
> API contract (`docs/SPEC-server-client.md` there) before this server existed
> in its current form — the two have diverged significantly. Nothing here is
> implemented yet; this is the plan. Both sides are open to change. Once a
> section is acted on, promote it into `SPEC.md` (server changes) and delete
> it from here, same convention as `IDEAS_TO_DEV.md`.

## TL;DR

- **Auth is already compatible** — no server change needed there.
- **Everything else the Swift client calls (stories/feeds/refresh/metrics/
  stream) targets routes and JSON shapes that don't exist on the real
  server.** Cheapest fix for most of these is updating the *Swift client* to
  match the real, richer server contract, not bending the server to match an
  invented one — the real server's shapes carry this project's invariants
  (per-user read state, story versioning, category taxonomy, etc.) that a
  generic reshaped API would lose.
- **The one genuinely missing *capability*** (not just a shape mismatch) is
  `POST /api/llm` — an LLM/embedding bridge so the Apple app's "self-hosted
  server" model tier can use this server's already-configured
  `LLM_BASE_URL`/`EMBED_BASE_URL` instead of talking to an upstream directly.
  This is worth building server-side. The Apple repo already has a detailed
  proposal for it (`docs/server-llm-endpoint.md`) — reusable for the request/
  response shape, but its FastAPI sketch should **not** be copied verbatim: it
  reimplements raw HTTP + its own bearer-token env var, duplicating
  `llm_client.py` and this server's existing auth. Wrap the existing
  `llm_client.chat_json()` / `llm_client.embed()` instead (see §5).

## Compatibility matrix

| Swift client call | Real server route | Gap | Fix side | Priority |
|---|---|---|---|---|
| Bearer token from Keychain | `POST /auth/login`, `/auth/setup`, `/auth/session-token` | None — client has no login UI yet, just consumes a pre-obtained token | Client (add login flow) | P1 |
| `GET /api/stories?page&pageSize` → `PagedResponse<StoryDTO>` | `GET /api/stories` (no pagination, different filters) | No pagination envelope; totally different item shape | Client (adapt to real shape); optional server: add `limit`/`offset` | P0 (shape), P2 (pagination) |
| `GET /api/stories/{id}` → `StoryDTO` | `GET /api/stories/{id}` → `StoryDetail` | Field mismatch (see §2) | Client | P0 |
| `GET /api/feeds` → `[FeedDTO]` | `GET /feeds` (admin-only) → `[FeedOut]` | Field mismatch + admin-only scoping | Client + server decision (§3) | P0 |
| `POST /api/feeds {url}` | `POST /feeds {url, ...}` (admin-only) | Path prefix (`/api` vs none), admin-only | Client | P0 |
| `DELETE /api/feeds/{id}` | `DELETE /feeds/{id}` (admin-only) | Path prefix, admin-only | Client | P0 |
| `POST /api/refresh` (global) | `POST /feeds/refresh` (all) / `POST /feeds/{id}/refresh` (one) | No top-level alias | Client (call existing route) | P1 |
| `GET /api/metrics` → `MetricsDTO` (incl. `totalCost`) | `GET /usage/summary`/`/daily`/`/by-feed` (admin-only, no cost — cost is client-side-only by design, invariant) | No single endpoint; no server-side cost | Client (compose from usage endpoints, drop `totalCost` or compute it client-side like the web GUI does) | P1 |
| `GET /api/stream` (SSE) → `{type, payload:[String:String], timestamp}` | `GET /activity/stream` → bare `data: {action, component, detail, ts}` (+ `{action:"ping"}` keepalive, one-off `event: hello`) | Different event envelope; no `id:`/replay/`retry:` | Client (parse real shape) | P0 |
| `POST /api/llm` (completion + embedding) | *(does not exist)* | Missing capability | **Server** (new endpoint) | **P0** |

## 1. Auth — already compatible

`app/core/security.py::make_session_token`/`parse_session_token` uses an
unsigned-expiry `URLSafeSerializer` token (no TTL beyond `SECRET_KEY`/user
existing), and `current_user` (`api/deps.py`) already accepts it as
`Authorization: Bearer <token>` — exactly what `NewsGatorClient.swift` sends.
`POST /auth/login` / `POST /auth/setup` return the token in the response body
(`AuthOut.token`), and `POST /auth/session-token` re-issues one for an
already-authenticated request (used today by the RSS feed and PWA flows).

**Nothing to change server-side.** The gap is entirely on the Apple side:
`NewsGatorServerClient` never calls any auth route — `AppServices.swift`
takes `serverToken` as an already-known string (Keychain-sourced) with no
code path that obtains it. **TODO (client):** add a login screen/flow that
calls `POST /auth/login` (or `/auth/setup` on first run against a fresh
server) and stores the returned `token` in the Keychain. No new server
capability required.

## 2. Stories — shape mismatch, not a capability gap

Real shapes (`backend/src/app/api/stories.py`): `StoryListItem` / `StoryDetail`
carry `id: int`, `headline` as `title`... — differences from Swift `StoryDTO`:

| Swift `StoryDTO` field | Real server field | Note |
|---|---|---|
| `id: String` | `id: int` | Swift should decode as `Int`, not `String` |
| `tags: [String]` | *(none)* | Not modeled server-side; drop or leave empty |
| `sentiment: String` | *(none)* | Not modeled; drop |
| `readingTimeMinutes: Int` | *(none)* | Not modeled; drop, or compute client-side from summary length |
| `canonicalURL` | *(none directly)* | Server's convention (used by RSS/Readeck/share) is "earliest-published source article's URL" — derive client-side from `source_hosts`/article list, or request server add it (see below) |
| *(missing)* | `category: str` | Taxonomy-driven category (see `GET /categories`) |
| *(missing)* | `image_url`, `source_hosts`, `is_read`, `updated_since_read`, `readeck_bookmark_id`, `version` | Core invariants (per-user read state §4, story versioning §3) the Swift model has no room for today |

**Recommendation:** rewrite `StoryDTO` (and `ArticleDTO`) to mirror
`StoryListItem`/`StoryDetail`/`ArticleOut` field-for-field (same names,
snake_case→camelCase via the existing `ServerJSON` conversion already handles
casing). This is the only way to carry per-user read state and story
versioning through to the client, which the app will need anyway for a
faithful "stories not articles" reading experience (this project's core UX
invariant). Pagination: the real endpoint returns the full filtered/sorted
list (already bounded by retention-window pruning); recommend the client drop
true pagination and do client-side windowing, OR (P2, low risk, additive) add
optional `limit`/`offset` query params server-side mirroring the existing
`GET /feed.xml`/`GET /activity/llm` `limit` convention.

**TODO (server, optional/P2):** add a `canonical_url` field to
`StoryListItem`/`StoryDetail` (earliest-published source article's URL —
same logic already implemented in `services/readeck.py`/`api/feed.py`) since
multiple consumers (RSS, Readeck, share, and now potentially the Apple
client) independently recompute this; consolidating it as a first-class field
removes duplicated logic. Not required to unblock the Apple client (it can
recompute from `source_hosts` + article list already returned).

## 3. Feeds — shape mismatch + an access-scope decision

Real `FeedOut` fields: `id`, `url`, `title`, `kind` (`rss`/`mail`),
`sender_email`, `backfill_days`, `story_count`, `unread_story_count`,
`email_count`. Swift `FeedDTO` invents `description`/`articleCount` that
don't exist server-side in that form.

**TODO (client):** align `FeedDTO` to `FeedOut`'s real fields; drop
`description` (not tracked) and `articleCount` (nearest equivalent is
`story_count`, which counts stories not raw articles — flag the semantic
difference in the DTO doc comment).

**Decision needed — feeds access scope:** `GET/POST/PATCH/DELETE /feeds` are
admin-only (`admin_user` dependency) on the real server, matching the web
GUI's admin-only Feeds page. The Swift client calls these with a plain
authenticated bearer token and no admin awareness. Two options:
1. **Keep admin-only** — document that the Apple app must authenticate as an
   admin user to manage feeds (fine for the common single-admin self-hosted
   deployment this project targets). *(Recommended — simplest, no server
   change, consistent with the web GUI.)*
2. Relax `GET /feeds` (read-only) to any authenticated user, mirroring the
   precedent already set by `GET /stories/feed-options` (any-user) — mutation
   routes (`POST`/`PATCH`/`DELETE`) stay admin-only.

No route path change needed either way — Swift just needs the `/api` prefix
stripped (`/api/feeds` → `/feeds`) or the base URL client passed without
`/api`, whichever is simpler in the client's `NewsGatorClient` init.

**TODO (client):** replace the invented `POST /api/refresh` (global) with the
real `POST /feeds/refresh` (all feeds) — the route already exists and does
exactly this; no server change needed. Per-feed refresh: `POST
/feeds/{id}/refresh`.

## 4. Metrics — no server change; compose from existing endpoints

Real usage endpoints (`api/usage.py`, admin-only): `GET /usage/summary?period=`,
`GET /usage/daily?days=`, `GET /usage/by-feed`. These return **token counts
and latency only** — cost is deliberately never computed server-side
(invariant: the web Usage page's price playground is client-side-only, so
prices can be compared live without a server setting). `MetricsDTO.totalCost`
has no server equivalent by design.

**TODO (client):**
- Drop `totalCost` from `MetricsDTO`, or compute it client-side from
  `usage/summary`'s per-model token counts using a user-configurable price
  table (same pattern as the web GUI) — do **not** ask the server to do this,
  it would be inconsistent with the existing invariant.
- `storyCount`/`feedCount`: derive from `GET /stories`/`GET /feeds` list
  lengths (no dedicated count endpoint exists or is needed for this).
- `averageSummarisationMs`-style figures: derivable from `usage/summary`'s
  `by_kind` rows (`latency_ms` sum ÷ `calls` for `kind == "summarize"`) — no
  server change needed, though a `tokens_per_s`-style derived field could be
  added there too if useful (P2, optional).

## 5. SSE activity stream — client should parse the real envelope

Real stream: `GET /activity/stream` (bearer/cookie/`?token=` auth via
`current_user`, same as everything else). Wire format is **not** SSE `event:`
per message (only a one-off `event: hello` on connect) — every subsequent
message is a bare `data: {...}\n\n` line, JSON-shaped as
`{action, component, detail, ts}` (mirrors `ActivityEvent`/`llmtrace`
payloads) or `{"action": "ping"}` as a 25s keepalive. There is **no** `id:`
field and **no** Last-Event-ID replay support — a reconnect just resumes the
live feed, missing whatever happened while disconnected (same behavior the
web GUI already lives with).

**TODO (client):** rewrite `NewsGatorClient+Events.swift`'s decode step to
match this shape instead of the invented generic `PipelineEventDTO {type,
payload: [String:String], timestamp}`. `detail` is arbitrary JSON (not a flat
string dict) — decode it as a generic JSON value (the Apple repo already has
exactly this in `JSONValue` from `NewsGatorCore/LanguageModel.swift`, reuse
it) rather than `[String: String]`. No server change needed; adding `id:`
line support for resumability would be a bigger lift (server would need to
replay a history buffer keyed by Last-Event-ID) — **not recommended** unless
a concrete need for gap-free reconnects emerges (P2 at most).

## 6. `POST /api/llm` — the one real capability gap (server work, P0)

This is genuinely missing and worth building: it lets the Apple app's
"self-hosted NewsGator server" model tier reuse *this* server's already
configured `LLM_BASE_URL`/`EMBED_BASE_URL` (oMLX/Ollama/llama.cpp/LM
Studio/cloud-compatible) instead of the Apple app needing its own separate
upstream LLM configuration. The Apple repo's `docs/server-llm-endpoint.md`
already has a well-thought-out request/response contract for 4 tasks
(`summary`, `rag`, `newsletter`, `embed`, per `NewsGatorCore/LanguageModel.swift`'s
`ModelTask`) — reuse that contract, but implement it as a thin adapter over
this server's existing `services/llm_client.py`, **not** a new raw-HTTP
upstream client:

- **Do not** add a second bearer-token scheme / env var (the sketch proposes
  `NEWSGATOR_BEARER_TOKEN` + `hmac.compare_digest`) — reuse the existing
  `current_user` dependency, same as every other route. Any authenticated
  user works (matches `/api/chat`'s existing scoping), not admin-only.
- **Do not** reimplement retries/JSON validation/timeouts via `urlopen` — for
  the 3 completion tasks (`summary`/`rag`/`newsletter`), call
  `llm_client.chat_json(system_prompt, input)` (already does JSON-mode +
  one retry on invalid JSON, per invariant "LLM calls go through the single
  client wrapper"). For `embed`, call `llm_client.embed([input])` and return
  the first vector.
- The client's `ModelRequest.responseSchema` is a JSON Schema hint for
  constrained decoding — `chat_json` doesn't do schema-constrained decoding
  today (relies on prompting + JSON-object mode). Fold the schema into the
  prompt sent to `chat_json` (append it to the system prompt as "respond with
  JSON matching this schema: ...") rather than trying to pass it through to
  the upstream unchanged; document that strict schema conformance is
  best-effort, matching how every other JSON-mode call site in this codebase
  already works.
- Response: `{"content": <json-encoded string of the parsed dict>, "usage":
  {...} | null}` — `content` as a JSON *string* (not a nested object) matches
  the Swift `CompletionResponse.content` `JSONValue`/string-decode path
  (`NewsGatorLanguageModel.swift`) with the least client-side change. `usage`
  should be `llm_client.last_usage.get()` translated to
  `{"prompt_tokens", "completion_tokens", "total_tokens"}`, or omitted
  (`None`) when the upstream didn't report it (already-existing behavior;
  Swift's `Usage` field is optional and unused by `generate()` today anyway).
  For `embed`, response is `{"embedding": [float, ...]}`.
- Wire it into existing conventions: `llmtrace.context("apple_bridge", label=task)`
  around the call (so the Activity page's LLM-interactions card shows these
  too, per invariant 6 — every pipeline/external-call transition emits an
  event), and `services/usage.py::record()` with new usage kinds
  (`apple_summary`/`apple_rag`/`apple_newsletter`/`apple_embed`) so these
  calls show up on the Usage page like every other LLM call in this system
  (invariant: all external LLM calls get one `llm_usage` row).
- Add a settings toggle, e.g. `APPLE_BRIDGE_ENABLED` (default on, whitelisted
  like every other setting per invariant 5) so a self-hoster who doesn't want
  to expose their configured LLM upstream to their own mobile app can turn it
  off; 404 the route when disabled (same pattern as `CHAT_ENABLED`).

**TODO (server):**
1. New router `backend/src/app/api/apple_llm.py` (or fold into an existing
   router) exposing `POST /api/llm`, `current_user`-scoped.
2. Pydantic request model matching the Apple `ModelRequest` wire shape
   (`task`, `system_prompt`, `input`, `options.{maximum_response_tokens,
   temperature}`) — confirm exact JSON keys against
   `NewsGatorServerClient`'s `ServerJSON` snake_case conversion before
   finalizing (Swift camelCase → snake_case is automatic, so `systemPrompt`
   → `system_prompt`, etc.).
3. Task dispatch: `summary`/`rag`/`newsletter` → `llm_client.chat_json`;
   `embed` → `llm_client.embed`.
4. `usage.record()` + `llmtrace.context()` wiring (see above).
5. `APPLE_BRIDGE_ENABLED` setting (`core/config.py` + `docker/.env.example`
   + settings API whitelist + Settings GUI, per invariant 5's
   "every new setting" rule).
6. Alembic: none needed (no new table — usage kinds are just string values in
   the existing `llm_usage.kind` column).
7. Update `SPEC.md` §6 endpoint table once implemented.

**TODO (client, once server lands the above):** adjust
`NewsGatorLanguageModel.swift`'s request encoding to match whatever exact
field names land server-side (should be a 1:1 match with `ModelRequest`, no
changes needed on the Swift side if the server mirrors it exactly);
`docs/server-llm-endpoint.md`'s raw-`urlopen` FastAPI sketch there should be
superseded by a link to this file / to the real server implementation once
built, so the Apple repo doesn't keep a stale, unused reference design.

## Suggested execution order

1. **P0 (unblocks basic sync):** Apple client DTO rewrite for stories/feeds
   to match real shapes (§2, §3) + SSE envelope fix (§5) + route path fixes
   (`/api/feeds` → `/feeds`, `/api/refresh` → `/feeds/refresh`). Pure client
   work, zero server changes.
2. **P0 (new capability):** server-side `POST /api/llm` bridge (§6).
3. **P1:** Apple login flow using existing `/auth/*` routes (§1); Metrics
   composed from `/usage/*` (§4).
4. **P2 (optional/future):** story `canonical_url` field, `/stories`
   pagination params, SSE resumability, feeds read-scope relaxation.
