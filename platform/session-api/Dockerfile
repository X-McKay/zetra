# The session interface. Build from the platform/ directory so the runner path dependency is
# in context:
#   docker build -f session-api/Dockerfile -t session-api platform/
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv
FROM python:3.12.14-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS build
COPY --from=uv /uv /usr/local/bin/uv
# The venv's scripts hardcode its path in their shebangs, so build it where it runs.
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY runner/pyproject.toml ./runner/
COPY runner/runner ./runner/runner
COPY session-api/pyproject.toml session-api/uv.lock ./session-api/
COPY session-api/session_api ./session-api/session_api
WORKDIR /src/session-api
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --compile-bytecode

FROM python:3.12.14-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
RUN groupadd --gid 65532 nonroot && useradd --uid 65532 --gid nonroot --no-create-home nonroot
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/app/.venv/bin/session-api"]
