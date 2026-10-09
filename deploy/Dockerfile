# Official multi-platform indexes, resolved and recorded on 2026-10-08.
FROM ghcr.io/astral-sh/uv:0.12.7@sha256:95f2aa1fe59274951cfe9b0cbc7972e879ff1004bc8945d130a32eb0dbd85945 AS uv
FROM docker.io/library/python:3.12.13-slim-trixie@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS builder

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/zetra-venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy
WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --locked --no-dev --extra temporal --no-editable --python /usr/local/bin/python

FROM docker.io/library/python:3.12.13-slim-trixie@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS runtime
LABEL org.opencontainers.image.title="Zetra read-only Temporal worker" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.description="Local reference worker; production write adapters are withheld"
ENV PATH=/opt/zetra-venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/nonexistent
WORKDIR /app
# Dependency installers and quality tools are absent from the runtime venv.
RUN python -m pip uninstall --yes pip
COPY --from=builder /opt/zetra-venv /opt/zetra-venv
COPY examples/knowledge /app/agent
RUN mkdir -p /app/agent/.zetra && chmod -R a-w /app /opt/zetra-venv
USER 10001:10001
ENTRYPOINT ["python", "-m", "zetra.temporal_worker", "--root", "/app/agent"]
