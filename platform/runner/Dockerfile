# Session Runner and cleanup reconciler image (spec 10.6, 10.7).
# Build from this directory: docker build -t agent-platform/session-runner .
FROM ghcr.io/astral-sh/uv:0.13.1 AS uv
FROM python:3.12.14-slim
COPY --from=uv /uv /usr/local/bin/uv
RUN groupadd --gid 1600 runner && useradd --uid 1600 --gid runner --no-create-home runner
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY runner ./runner
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable \
    --compile-bytecode
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 1600:1600
# --role sessions (Deployment session-runner-<tenant>) or --role reconciler; TENANT from env.
ENTRYPOINT ["/app/.venv/bin/agent-session-runner"]
CMD ["--role", "sessions"]
