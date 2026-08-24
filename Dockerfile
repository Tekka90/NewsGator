# syntax=docker/dockerfile:1

# --- frontend build ---
FROM node:22-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- backend runtime ---
FROM python:3.12-slim AS backend
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL=sqlite+aiosqlite:////data/newsgator.db
WORKDIR /app

RUN pip install --no-cache-dir uv
COPY backend/pyproject.toml ./
COPY backend/src ./src
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
RUN uv pip install --system --no-cache .

# frontend bundle served by adapter-node
COPY --from=frontend /app/frontend/build ./frontend/build
COPY --from=frontend /app/frontend/package.json ./frontend/package.json

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME /data
EXPOSE 8000 3000
ENTRYPOINT ["/entrypoint.sh"]
