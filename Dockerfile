FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir ".[postgres]"

RUN useradd --create-home --uid 10001 mycoloop && chown -R mycoloop /app
USER mycoloop

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
  CMD curl -fsS http://localhost:8000/healthz || exit 1

# Migrations are an explicit deploy step, not an import side effect.
CMD ["sh", "-c", "alembic upgrade head && uvicorn mycoloop.main:app --host 0.0.0.0 --port 8000"]
