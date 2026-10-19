FROM ghcr.io/astral-sh/uv:0.13.1 AS uv
FROM python:3.12.14-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
# Locked SDK dependencies may come from Git; Git is only a build dependency.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY agent/ ./
# Cache trusted code at build time, never a running agent or session workspace.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable --compile-bytecode

FROM python:3.12.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 nftables \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1500 agent \
    && useradd --uid 1500 --gid agent --create-home agent \
    && install -d -o agent -g agent /sandbox /app
# Preserve the absolute virtualenv location used during dependency installation.
COPY --from=builder /app /app
RUN /app/.venv/bin/python -m compileall -q /usr/local/lib/python3.12
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /sandbox
USER agent
CMD ["/app/.venv/bin/python", "-c", "import time; time.sleep(1800)"]
