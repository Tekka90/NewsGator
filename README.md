# NewsGator

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Self-hosted, multi-user news reader that clusters articles about the same event into
**Stories** using an external OpenAI-compatible LLM.

🌐 **[Project page](https://tekka90.github.io/NewsGator/)** — why it exists, the
philosophy behind it, and how it feels to use.

## Features

- **Stories, not article lists** — RSS feeds are ingested, full-text fetched
  (trafilatura + readability fallback), summarized in your language, embedded, and
  clustered into Stories with a merged summary that versions as new facts arrive.
- **Newsletters are feeds too** — point a per-user IMAP account + folder at your
  newsletter mailbox (Settings) and every sender becomes a feed: article links are
  extracted from each message (code-first parsing, then two LLM passes — one
  deletes the intro/socials/sponsor chrome from the full email using placeholder
  link tokens, one recovers the human-written intro text per surviving link),
  then processed like any RSS entry. New stories show
  the newsletter's own intro instead of the machine summary, and the article's
  publication date comes from the linked page's explicit metadata
  (og/JSON-LD) when it exists — otherwise the email's date (never guessed from
  URLs or copyright lines).
- **Your LLM, your data** — works with any OpenAI-compatible server (oMLX, Ollama,
  llama.cpp, LM Studio…). Nothing is hardcoded to a provider; articles never leave
  infrastructure you chose.
- **Multi-user with per-user read state** — read stories that receive new facts come
  back as "updated", never as unread noise. Filter the story list by feed, and see
  per-feed story/unread counts on the Feeds page.
- **Ask your archive** — a built-in chatbot answers questions across everything
  you've loaded (RAG): your question is embedded and matched against story
  centroids, the top story summaries ground the answer, and cited stories are
  rendered as clickable cards. History is stored per user, so it follows you
  across devices.
- **Full visibility** — live activity stream (SSE) for every pipeline stage, a live
  LLM interaction trace (see each prompt and its reply as it happens), and LLM
  token-usage metrics per day/stage/model/feed with a price playground.
- **Quality-of-life** — PWA (installable, works on iOS), dark mode, mobile swipe
  deck, OPML import, favicons (feed icons included — with homepage `<link rel=icon>`
  and parent-domain fallback for newsletter sender domains), story sharing with
  on-demand translation, your Stories re-exposed as an RSS feed
  (`GET /api/feed.xml`), optional Readeck integration.
- **Simple to run** — one Docker container, SQLite by default (sqlite-vec for
  vectors), optional external Qdrant.

| Stories (desktop) | Story detail | Swipe deck (mobile) |
| --- | --- | --- |
| ![Stories list](docs/assets/shots/stories-desktop.png) | ![Story detail](docs/assets/shots/story-detail.png) | ![Mobile deck](docs/assets/shots/deck-mobile.png) |

See [SPEC.md](SPEC.md) for the normative spec and
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the milestone checklist.

## Native Apple Client vs Web App (In-App Source Browsing)

There is a deliberate UX difference in how source articles are viewed between the native Apple clients (`NewsGator-Apple` for iOS/macOS) and the Web app:

- **iOS / macOS Native Apps (`NewsGator-Apple`)**: Feature a tabbed story detail view where each source article has its own dedicated tab rendering the live web page directly inside the app using native `WKWebView`. Because `WKWebView` runs in an independent, top-level window context, it behaves like a standalone browser visit and is not subject to cross-origin iframe security restrictions.
- **Web App (`Newsgator`)**: In a web browser, embedding third-party publisher websites in an `<iframe>` is blocked by modern news outlets (e.g. France 24, BBC, The New York Times, Le Monde) via `X-Frame-Options: SAMEORIGIN` or CSP `frame-ancestors` HTTP headers designed to prevent clickjacking. Web browsers enforce these policies by showing security errors (Firefox/Zen) or blank screens (Mobile Safari/Chrome). Therefore, the Web app provides the full story details and sources list with direct external links that open publisher pages in standard browser tabs instead.

## Docker (production)

Using the official image:

```bash
cd docker
cp .env.example .env   # set SECRET_KEY + LLM_BASE_URL for your LLM server
docker compose up
```

Or build from source:

```bash
cd docker
cp .env.example .env   # set SECRET_KEY + LLM_BASE_URL for your LLM server
docker compose up --build
```

Then open http://localhost:3000 — first run asks you to create the admin account.
The LLM is an external OpenAI-compatible server (oMLX, Ollama, llama.cpp, LM Studio…);
point `LLM_BASE_URL` at it. Qdrant is optional and external (`VECTOR_BACKEND=qdrant`
+ `QDRANT_URL`) — never spun up by this project.

## Development quickstart

```bash
# Backend (http://localhost:8000, OpenAPI docs at /docs)
cd backend
pip install -e '.[dev]'
uvicorn app.main:app --reload

# Frontend (http://localhost:5173, proxies /api to :8000)
cd frontend
npm install
npm run dev
```

First run: open the GUI → you'll be redirected to create the admin account, then add
feeds on the Feeds page.

## Tests & checks

```bash
cd backend && pytest && ruff check src tests && mypy src
cd frontend && npm run check && npm run build
```

## Contributing & security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and ground rules,
and [SECURITY.md](SECURITY.md) for how to report a vulnerability.

## License

[MIT](LICENSE)
