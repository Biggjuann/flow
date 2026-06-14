FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependency layer (cached unless pyproject/lock change)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# App layer
COPY adengine/ adengine/
COPY prompts/ prompts/
COPY README.md ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    ADENGINE_RUNS_DIR=/app/runs

EXPOSE 8000

CMD ["sh", "-c", "uvicorn adengine.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
