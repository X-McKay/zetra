# Session Runner and cleanup reconciler image (spec 10.6, 10.7).
# Build from this directory: docker build -t agent-platform/session-runner .
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv
FROM python:3.12.14-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
COPY --from=uv /uv /usr/local/bin/uv
RUN groupadd --gid 1600 runner && useradd --uid 1600 --gid runner --no-create-home runner
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY runner ./runner
# A source-only change does not invalidate uv's cached local-project wheel by default.
# Rebuild it explicitly: console scripts import site-packages, not /app/runner.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable \
    --reinstall-package agent-platform-runner --compile-bytecode
# Verify the package the entrypoints actually import, with the source directory off sys.path.
RUN /app/.venv/bin/python -I -c \
    'from pathlib import Path; import runner; \
    files = lambda root: {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*.py")}; \
    assert files(Path("/app/runner")) == files(Path(runner.__file__).parent), "stale runner install"'
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 1600:1600
# --role sessions (Deployment session-runner-<tenant>) or --role reconciler; TENANT from env.
ENTRYPOINT ["/app/.venv/bin/agent-session-runner"]
CMD ["--role", "sessions"]
