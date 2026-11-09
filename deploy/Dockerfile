# Roomba Kit service image (research R11). Build from the repo root:
#   docker build -f deploy/Dockerfile -t roomba-kit .
# Run it with deploy/compose.yaml: discovery needs the host network.

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so source edits don't re-resolve them.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
RUN groupadd --gid 1000 roomba \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin roomba \
    && install -d -o roomba -g roomba -m 0700 /config /state
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    ROOMBA_KIT_CONFIG_DIR=/config \
    ROOMBA_KIT_STATE_DIR=/state \
    ROOMBA_KIT_SERVER__HOST=0.0.0.0
VOLUME ["/config", "/state"]
USER roomba
EXPOSE 8765
# No curl in slim: ask the kit itself, which honours server.port from env or config.toml.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys; from roomba_kit.cli.client import local_service_url; from roomba_kit.lifecycle.daemon import health; sys.exit(health(local_service_url(), timeout=4) is None)"]
CMD ["roomba-kit", "serve"]
