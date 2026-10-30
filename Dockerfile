# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Optional cloud proxy CA; normal local builds use the image system trust.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --frozen --no-dev
COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 migrations ./migrations
RUN useradd --create-home --uid 10001 bot
USER bot
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
CMD ["python", "-m", "app", "web"]
