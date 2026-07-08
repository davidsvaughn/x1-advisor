# X1 Advisor service (Phase 5). Cloud Run image, uv-based install.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 UV_SYSTEM_PYTHON=1
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY x1_advisor ./x1_advisor

# One worker per container instance = one DB connection (per-worker-store
# decision, DECISIONS 2026-07-07); scale via Cloud Run instances, not workers.
# Cloud SQL: set ADVISOR_PGHOST to the connector socket dir at deploy.
CMD ["uv", "run", "--no-sync", "uvicorn", "x1_advisor.service:app", \
     "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
