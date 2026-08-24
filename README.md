# NewsGator

Self-hosted, multi-user news reader that clusters articles about the same event into
**Stories** using an external OpenAI-compatible LLM.

See [SPEC.md](SPEC.md) for the normative spec and
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the milestone checklist.

## Docker (production)

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
