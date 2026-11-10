# docker build -t netpol-attestor platform/netpol-attestor
# Runs with hostNetwork and NET_ADMIN/NET_RAW (5.5). Reading the netfilter tables needs
# CAP_NET_ADMIN in the host network namespace, so the process runs as uid 0 with every
# other capability dropped by the DaemonSet. Set ATTESTOR_IPTABLES_SAVE to
# iptables-legacy-save on nodes whose kube-router uses the legacy backend (17.3).
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv
FROM python:3.12.14-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY netpol_attestor ./netpol_attestor
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --compile-bytecode

FROM python:3.12.14-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
RUN apt-get update && apt-get install -y --no-install-recommends iptables \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ATTESTOR_IPTABLES_SAVE=iptables-save
ENTRYPOINT ["/app/.venv/bin/netpol-attestor"]
