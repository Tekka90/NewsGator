# NewsGator

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Self-hosted, multi-user news aggregator that clusters every article about the same
real-world event into one living **Story** — neutral summary, honest clickbait-free
title, and a link back to the original publishers — using an external
OpenAI-compatible LLM.

The server comes with a web app (desktop + touch-first mobile deck, installable PWA).
Official native iOS/macOS apps are also available, either as clients of this server or
as standalone apps, but they are optional: the web app is fully usable on its own.

🌐 **[Project page](https://www.newsgator.app/)** — why it exists, the
philosophy behind it, and how it feels to use.

## Features

- **Stories, not article lists** — articles are ingested, full-text fetched
  (trafilatura + readability fallback), summarized in your language, embedded, and
  clustered into Stories with a merged summary and a factual title that version as
  new facts arrive.
- **Three ingestion channels** — RSS feeds, IMAP newsletters (below), and
  third-party reader accounts through the Google Reader API (FreshRSS, Miniflux,
  Inoreader, The Old Reader, BazQux) with bidirectional read-state sync.
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
  deck, OPML import/export, categories, favicons (feed icons included — with homepage `<link rel=icon>`
  and parent-domain fallback for newsletter sender domains), story sharing with
  on-demand translation, your Stories re-exposed as an RSS feed
  (`GET /api/feed.xml`), optional Readeck integration.
- **Simple to run** — one Docker container, SQLite by default (sqlite-vec for
  vectors), optional external Qdrant.

| Mac — Stories | iPhone — Stories | Mac — Story detail |
| --- | --- | --- |
| ![Mac stories view](docs/assets/shots/app-mac-stories.jpg) | ![iPhone stories view](docs/assets/shots/app-iphone-stories.jpg) | ![Mac story detail](docs/assets/shots/app-mac-story-detail.jpg) |

| Mac — Activity | Web app — LLM usage |
| --- | --- |
| ![Mac activity view](docs/assets/shots/app-mac-activity.jpg) | ![Web app LLM usage](docs/assets/shots/app-web-usage.jpg) |

See [SPEC.md](SPEC.md) for the normative spec and
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the milestone checklist.

## Apple apps (optional)

Official native iOS/macOS apps exist for NewsGator. They can connect to this server,
but they are not required: everything the server offers is available in the web app.
In the web app, source articles open on the publisher's site in a new browser tab,
because most publishers block embedding via `X-Frame-Options` / CSP `frame-ancestors`.

## Docker (production)

Using the official image:

```bash
cd docker
cp .env.example .env   # set SECRET_KEY + LLM_BASE_URL for your LLM server
docker compose up
```

Or build from source: in `docker/docker-compose.yml`, comment out `image:` and
uncomment the `build:` section, then run `docker compose up --build`.

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
