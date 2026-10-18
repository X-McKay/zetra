FROM ghcr.io/astral-sh/uv:0.13.1 AS uv
FROM python:3.12.14-slim
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 nftables git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1500 agent \
    && useradd --uid 1500 --gid agent --create-home agent \
    && install -d -o agent -g agent /sandbox /app
RUN mkdir /private-canary /restricted-write && echo denied > /private-canary/readable \
    && chmod 755 /private-canary && chmod 444 /private-canary/readable && chmod 777 /restricted-write
WORKDIR /app
COPY agent/ ./
# Cache trusted code at build time, never a running agent or session workspace.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable --compile-bytecode
RUN /app/.venv/bin/python -m compileall -q /usr/local/lib/python3.12
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /sandbox
USER agent
CMD ["/app/.venv/bin/python", "-c", "import time; time.sleep(1800)"]
