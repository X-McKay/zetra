set shell := ["bash", "-euo", "pipefail", "-c"]
export UV_CACHE_DIR := justfile_directory() / ".tools/uv-cache"

default:
    @just --list
bootstrap:
    uv sync --frozen
lint:
    uv run --frozen ruff check src tests scripts benchmarks examples
    uv run --frozen ruff format --check src tests scripts benchmarks examples
check: lint
    uv lock --check
    uv run --frozen pytest -m 'not cluster' -q
    uv run --frozen python scripts/check_docs.py
    uv run --frozen python -m zetra test examples/workspace_reporter
    uv run --frozen python -m zetra test examples/document_reader
    uv run --frozen python -m zetra eval examples/workspace_reporter --mode fixture
    uv run --frozen python -m zetra eval examples/document_reader --mode fixture
cluster-up:
    bash scripts/kind-up.sh
qualify:
    uv run --frozen zetra qualify
latency:
    uv run --frozen zetra qualify --suite latency
platform-check:
    for p in contracts controller netpol-attestor agent-runtime egress-guard runner; do \
        (cd platform/$p && uv sync --frozen && uv run ruff check . && uv run ruff format --check . \
            && uv run pytest -m 'not cluster' -q); \
    done
    cd platform/aps && go vet ./... && go test ./...
