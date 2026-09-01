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
- **Your LLM, your data** — works with any OpenAI-compatible server (oMLX, Ollama,
  llama.cpp, LM Studio…). Nothing is hardcoded to a provider; articles never leave
  infrastructure you chose.
- **Multi-user with per-user read state** — read stories that receive new facts come
  back as "updated", never as unread noise.
- **Ask your archive** — a built-in chatbot answers questions across everything
  you've loaded (RAG): your question is embedded and matched against story
  centroids, the top story summaries ground the answer, and cited stories are
  rendered as clickable cards. History is stored per user, so it follows you
  across devices.
- **Full visibility** — live activity stream (SSE) for every pipeline stage, and LLM
  token-usage metrics per day/stage/model/feed with a price playground.
- **Quality-of-life** — PWA (installable, works on iOS), dark mode, mobile swipe
  deck, OPML import, favicons, story sharing with on-demand translation, optional
  Readeck integration.
- **Simple to run** — one Docker container, SQLite by default (sqlite-vec for
  vectors), optional external Qdrant.

| Stories (desktop) | Story detail | Swipe deck (mobile) |
| --- | --- | --- |
| ![Stories list](docs/assets/shots/stories-desktop.png) | ![Story detail](docs/assets/shots/story-detail.png) | ![Mobile deck](docs/assets/shots/deck-mobile.png) |

See [SPEC.md](SPEC.md) for the normative spec and
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the milestone checklist.

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
