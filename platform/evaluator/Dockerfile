# The Agent Registry's evaluator, one per cluster. Build from this directory:
#   docker build -t registry-evaluator platform/evaluator
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv
FROM python:3.12.14-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS build
COPY --from=uv /uv /usr/local/bin/uv
# The venv's scripts hardcode its path in their shebangs, so build it where it runs.
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY registry_evaluator ./registry_evaluator
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --compile-bytecode
RUN cd / && /app/.venv/bin/python -I -c \
    'import registry_evaluator as p; assert not p.__file__.startswith("/src"), p.__file__'

FROM python:3.12.14-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
RUN groupadd --gid 65532 nonroot && useradd --uid 65532 --gid nonroot --no-create-home nonroot
COPY --from=build /app/.venv /app/.venv
# No liveness probe: one pass lasts as long as a suite does. The registry's status names an
# evaluation left pending for ten minutes (contract/delivery.md section 10).
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
ENTRYPOINT ["/app/.venv/bin/registry-evaluator"]
