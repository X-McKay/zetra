# Agents built with Zetra for the Kubani Agent Runtime Platform.
# Rendered by `zetra-agents render`; do not edit. Repository-specific recipes are not
# supported in this file; ask for a template change in zetra platform/agent-tools.

set positional-arguments

default:
    @just --list

# Install the locked workspace: every agent's dependencies and the tooling.
setup:
    uv sync --locked --all-packages

# Lint, test and layout-check the repository and every agent.
check:
    uv run --locked ruff check .
    uv run --locked ruff format --check .
    uv run --locked zetra-agents check
    uv run --locked --all-packages zetra-agents render --check
    agents=$(uv run --locked zetra-agents list) || exit $?; for a in $(printf '%s' "$agents" | tr -d '[],"'); do just check-agent "$a" || exit $?; done

# Rewrite what is derived: the files rendered from zetra, and each agent.yaml `tools:`
# list from the tools its code declares.
render:
    uv run --locked --all-packages zetra-agents render

# Test one agent.
check-agent agent:
    uv run --locked pytest -q "agents/$1"

# Run one session of an agent on this machine: a scripted or local model and scripted MCP
# replies from agents/AGENT/scenarios/ (default: smoke). Not a deployment check.
run agent *args:
    uv run --locked --all-packages zetra-agents run "$@"

# Build one agent's image locally (not pushed), with the target its kind requires.
build agent:
    docker build --platform linux/amd64 --build-arg "AGENT=$1" --target "$(uv run --locked zetra-agents image-target "$1")" -t "agent-$1:dev" .

# Print the compiled policy bundle and digest for a release file, as the controller computes it.
policy-digest agent release:
    uv run --locked agentctl policy-digest --agent "agents/$1/deploy/agent.yaml" --release "$2" --mcp-resource-base https://mcp.almckay.io --print-bundle

# Offline source/candidate compatibility against a Kubani checkout before tagging.
preflight agent tag kubani:
    uv run --locked zetra-agents preflight "$1" --tag "$2" --kubani "$3"
