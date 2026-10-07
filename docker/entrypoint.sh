#!/bin/sh
set -e

# DB schema: Alembic migrations run automatically in the app lifespan at
# startup (upgrade head; legacy create_all DBs are stamped then upgraded).

# backend (API on :8000)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

# Don't start the frontend until the backend answers (migrations run in its
# lifespan), otherwise the proxy logs a burst of "fetch failed" 500s at boot.
i=0
until python -c "import urllib.request as u; u.urlopen('http://localhost:8000/api/health', timeout=2)" 2>/dev/null; do
    kill -0 "$BACKEND_PID" 2>/dev/null || exit 1
    i=$((i+1)); [ "$i" -ge 300 ] && break
    sleep 1
done

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
