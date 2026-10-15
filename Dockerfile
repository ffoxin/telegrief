FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /bin/

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --home-dir /tmp --shell /usr/sbin/nologin app

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

RUN mkdir -p /app/data && chown app:app /app/data
VOLUME ["/app/data"]
USER app

CMD ["telegrief", "serve"]
