#!/bin/sh
set -e

# Run DB migrations (Alembic owns the schema from M8 on; create_all stays as a
# no-op safety net for the very first run before revisions apply)
cd /app
python -m alembic upgrade head || echo "alembic upgrade failed — continuing with create_all fallback"

# backend (API on :8000)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 &

# frontend (adapter-node on :3000, /api proxied to backend)
cd /app/frontend
PORT=3000 HOST=0.0.0.0 BACKEND_URL=http://localhost:8000 node build &

wait -n
exit $?
