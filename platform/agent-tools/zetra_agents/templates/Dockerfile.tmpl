# syntax=docker/dockerfile:1.7-labs@sha256:b99fecfe00268a8b556fad7d9c37ee25d716ae08a5d7320e6d51c4dd83246894
# Agent image (spec 11.1, 14.1). Rendered by `zetra-agents render` from zetra
# platform/agent-tools; do not edit. Build from the repository root:
#   docker build --build-arg AGENT=<name> --target "$(zetra-agents image-target <name>)" .
# The pod template runs /app/.venv/bin/python -m agent_runtime.wait as UID 1500 with a
# read-only root filesystem; the Session Runner execs agent_runtime.step per boundary.
FROM ghcr.io/astral-sh/uv:0.13.1 AS uv

FROM python:3.12.16-slim@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07 AS build
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ARG AGENT
# The workspace lock is the image lock (AgentRelease.spec.lockSha256); only this agent's
# dependencies are installed from it.
COPY pyproject.toml uv.lock ./
COPY --parents agents/*/pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package "zetra-agent-${AGENT}" --compile-bytecode

# 3.12.16 fixes the interpreter's Critical CVE-2026-19445 and CVE-2026-115557; the
# upgrade takes Debian security fixes newer than the base image (openssl, pcre2), which
# Kubani's admission vuln check would otherwise reject.
FROM python:3.12.16-slim@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07 AS runtime
RUN apt-get update && apt-get -y upgrade --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
ARG AGENT
COPY --from=build /app/.venv /app/.venv
COPY agents/${AGENT}/agent.yaml /app/agent.yaml
COPY agents/${AGENT}/src/ /app/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 1500:1500
ENTRYPOINT ["/app/.venv/bin/python", "-m", "agent_runtime.wait"]

# Conformance fixtures only (zetra_agents.layout.FIXTURES): the agent package's step.py
# replaces the installed SDK boundary driver, which stays available to it as
# agent_runtime._<name>_sdk_step, and the agent's verify_image.py must pass against the
# installed module before the image exists. The ordinary SDK manifest and factory
# validators are preserved; no validation is skipped.
FROM runtime AS fixture
ARG AGENT
USER 0:0
COPY agents/${AGENT}/verify_image.py /tmp/verify_image.py
RUN /app/.venv/bin/python - <<'PY'
import pathlib, re, shutil
import agent_runtime.step
installed = pathlib.Path(agent_runtime.step.__file__)
drivers = sorted(pathlib.Path("/app").glob("*/step.py"))
assert len(drivers) == 1, drivers
backup = re.search(r"agent_runtime\._([a-z]+)_sdk_step", drivers[0].read_text()).group(1)
shutil.copyfile(installed, installed.with_name(f"_{backup}_sdk_step.py"))
shutil.copyfile(drivers[0], installed)
shutil.rmtree(installed.parent / "__pycache__", ignore_errors=True)
PY
RUN /app/.venv/bin/python -I /tmp/verify_image.py && rm /tmp/verify_image.py
LABEL agentplatform.io.conformance-fixture="${AGENT}"
USER 1500:1500
