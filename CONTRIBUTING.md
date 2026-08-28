# Contributing

Thanks for your interest! NewsGator is a small project — issues and pull requests
are welcome.

## Before you start

- Read [SPEC.md](SPEC.md) — it is the normative specification. Changes that alter
  architecture, pipeline behavior, the data model, or the API must keep SPEC.md in
  sync in the same PR.
- Skim [.github/copilot-instructions.md](.github/copilot-instructions.md) for the
  project invariants (story versioning, per-user read state, activity events,
  resumability, configurability rules…). Breaking an invariant is the fastest way
  to get a PR sent back.

## Development setup

```bash
# Backend (Python 3.12+)
cd backend
pip install -e '.[dev]'
uvicorn app.main:app --reload   # http://localhost:8000

# Frontend (Node 20+)
cd frontend
npm install
npm run dev                     # http://localhost:5173, proxies /api to :8000
```

## Checks — all must pass

```bash
cd backend && pytest && ruff check src tests && mypy src
cd frontend && npm run check && npm run build
```

## Ground rules

- **Tests**: every behavior change comes with pytest coverage (mock the LLM client —
  tests never hit a real server).
- **Migrations**: every schema change = one Alembic revision, committed with the code.
- **Config**: new settings go in `core/config.py` **and** `docker/.env.example`
  (commented out, one-line doc) in the same change.
- **API changes**: update the endpoint table in SPEC.md §6.
- **Pipeline stages**: emit a structured activity event (see the activity service).
- **No model-serving code**: the LLM is always an external OpenAI-compatible server.
  Never hardcode a provider.
- Keep PRs focused. One feature or fix per PR beats a grab-bag.

## Reporting bugs

Open an issue with: what you did, what you expected, what happened, and the relevant
`ACTIVITY_LOG` entries (Activity page in the GUI). For security issues, see
[SECURITY.md](SECURITY.md) — do not open a public issue.
