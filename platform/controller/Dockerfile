# Build from the platform/ directory so the contracts path dependency is in context:
#   docker build -f controller/Dockerfile -t agent-registry-controller platform/
FROM ghcr.io/astral-sh/uv:0.13.1 AS uv
FROM python:3.12.14-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /src
COPY contracts ./contracts
COPY controller/pyproject.toml controller/uv.lock ./controller/
COPY controller/registry_controller ./controller/registry_controller
WORKDIR /src/controller
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --compile-bytecode

# cosign is the verifier in both profiles (6.4 AgentRelease step 1); pin by checksum.
FROM python:3.12.14-slim AS cosign
ARG COSIGN_VERSION=v2.6.1
ARG COSIGN_SHA256
ADD https://github.com/sigstore/cosign/releases/download/${COSIGN_VERSION}/cosign-linux-amd64 /cosign
RUN test -n "$COSIGN_SHA256" && echo "$COSIGN_SHA256  /cosign" | sha256sum -c - && chmod 0555 /cosign

FROM python:3.12.14-slim
RUN groupadd --gid 65532 nonroot && useradd --uid 65532 --gid nonroot --no-create-home nonroot
COPY --from=build /src/controller/.venv /app/.venv
COPY --from=cosign /cosign /usr/local/bin/cosign
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/app/.venv/bin/agent-registry-controller"]
