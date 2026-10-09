set shell := ["bash", "-euo", "pipefail", "-c"]
export UV_CACHE_DIR := justfile_directory() / ".tools/uv-cache"
export PREK_HOME := justfile_directory() / ".tools/prek-cache"

# List the supported contributor commands.
default:
    @just --list

# Install exact runtime, Temporal and development dependencies.
bootstrap:
    uv sync --locked --all-extras

# Confirm the committed resolution matches pyproject.toml.
lock-check:
    uv lock --check

# Rewrite Python formatting and safe lint fixes.
format:
    uv run --locked --all-extras ruff check --fix .
    uv run --locked --all-extras ruff format .

# Check Python formatting and lint without changing files.
lint:
    uv run --locked --all-extras ruff check .
    uv run --locked --all-extras ruff format --check .

# Check public contracts, tests and examples against Python 3.11.
typecheck:
    uv run --locked --all-extras ty check

# Validate GitHub Actions syntax, expressions and embedded shell commands.
ci-lint:
    actionlint

# Check all integration and build shell scripts without running them.
shell-lint:
    shellcheck integrations/*.sh scripts/*.sh

# Build the ARM64 lab worker image on the explicit disposable Podman connection.
worker-image:
    bash scripts/build_worker_image.sh

# Load the lab worker into kind-zetra-validation and record its OCI digest.
worker-load:
    bash scripts/load_worker_image.sh

# Run deterministic toolkit tests; no live cluster credentials are required.
test:
    uv run --locked --all-extras python -m unittest discover -s tests -v

# Build the combined book and all standalone HTML chapters.
docs:
    uv run --locked python scripts/build_playbooks.py
    uv run --locked python scripts/check_docs.py

# Preview public tracked documents and linked evidence on loopback only.
preview:
    uv run --locked python scripts/serve_docs.py

# Validate source contracts and evaluate each executable example.
examples:
    uv run --locked zetra check examples/knowledge
    uv run --locked zetra eval examples/knowledge --output .tools/knowledge-evidence.json
    uv run --locked zetra check examples/knowledge --evidence .tools/knowledge-evidence.json
    uv run --locked zetra eval examples/action --output .tools/action-evidence.json
    uv run --locked zetra check examples/action --evidence .tools/action-evidence.json
    uv run --locked zetra eval examples/gateway-read --output .tools/gateway-evidence.json
    uv run --locked zetra check examples/gateway-read --evidence .tools/gateway-evidence.json
    uv run --locked zetra eval examples/incident-triage --output .tools/triage-evidence.json
    uv run --locked zetra check examples/incident-triage --evidence .tools/triage-evidence.json
    just incident-triage
    just developer-starter

# Run the neutral document-assistant fixture, tests and scored example cases.
developer-starter:
    uv run --locked python examples/developer-starter/run.py --question 'What should I do before submitting expenses?' --document expense-policy
    PYTHONPATH=examples/developer-starter/src uv run --locked python -m unittest discover -s examples/developer-starter/tests -v
    uv run --locked python examples/developer-starter/evaluate.py --output build/developer-starter-evaluation.json

# Follow one incident through proposal, local approval, ticket receipt and retries.
incident-triage:
    PYTHONPATH=src:examples/incident-triage/src uv run --locked python examples/incident-triage/demo.py

# Run all repository gates. Live enforcement remains a separate qualification.
check: lock-check lint typecheck ci-lint shell-lint test docs examples

# Check pre-commit configuration; does not install hooks.
hooks-config:
    uv run --locked prek validate-config .pre-commit-config.yaml

# Run hooks against Git-tracked files; requires a Git checkout.
hooks-check:
    uv run --locked --all-extras prek run --all-files

# Evaluate the development shells without building every architecture.
nix-check:
    nix flake check --no-build --all-systems

# Upgrade the Python resolution deliberately; review the lockfile diff.
upgrade:
    uv lock --upgrade
    uv sync --locked --all-extras

# Re-index recorded live reports; does not run or authorize cluster mutations.
evidence-index:
    uv run --locked python scripts/build_validation_report.py

# Exercise synthetic kernel/CNI fixtures in the dedicated local kind context.
lab-kernel:
    uv run --locked python integrations/kernel-network-qualify.py --kubeconfig .tools/zetra-kubeconfig --output integrations/evidence/kernel-network-report.json

# Create, test and remove a strict local OpenShell sandbox.
lab-openshell:
    uv run --locked python integrations/openshell-reproduce.py

# Exercise the real gateway/Temporal/collector chain in the local lab.
lab-gateway:
    bash integrations/gateway-reproduce.sh

# Install or verify the pinned disposable platform prerequisites.
lab-bootstrap:
    bash integrations/bootstrap-local-platform.sh

# Ask the local API server to validate a withheld candidate; never start it.
lab-render:
    uv run --locked python integrations/render-qualify.py
