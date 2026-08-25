#!/bin/sh
set -e

# DB schema: Alembic migrations run automatically in the app lifespan at
# startup (upgrade head; legacy create_all DBs are stamped then upgraded).

# backend (API on :8000)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

# frontend (adapter-node on :3000, /api proxied to backend)
cd /app/frontend
PORT=3000 HOST=0.0.0.0 BACKEND_URL=http://localhost:8000 node build &
FRONTEND_PID=$!

# POSIX-compatible: exit the container when either process dies so Docker's
# restart policy can take over. (dash's `wait` has no `-n`; poll instead.)
while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
    sleep 2
done

# one of them died — propagate a non-zero status
exit 1
