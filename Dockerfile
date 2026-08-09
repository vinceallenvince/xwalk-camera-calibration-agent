FROM python:3.12-slim AS base

WORKDIR /app

# uv resolves and installs from the lockfile; --no-dev keeps pytest and pillow
# (test-only) out of the runtime image.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX=https://pypi.org/simple \
    PYTHONUNBUFFERED=1 \
    PORT=8080

COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY app ./app

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
