#!/bin/sh
set -e

# DB schema: Alembic migrations run automatically in the app lifespan at
# startup (upgrade head; legacy create_all DBs are stamped then upgraded).

# backend (API on :8000)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 &

# frontend (adapter-node on :3000, /api proxied to backend)
cd /app/frontend
PORT=3000 HOST=0.0.0.0 BACKEND_URL=http://localhost:8000 node build &

wait -n
exit $?
