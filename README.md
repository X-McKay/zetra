# Zetra — ZEro-TRust Agents

A comprehensive Agent Playbook and an initial framework-neutral Python toolkit for building, evaluating, deploying and governing agents with explicit authority.

Start with the [complete HTML playbook](docs/index.html), or choose an audience:

- [Agent Strategy](docs/strategy.html) — business outcomes, success measures, a unified lifecycle and platform operating model.
- [Agent Developer Playbook](docs/developer.html) — package structure, typed factories, evaluations, security and developer tools.
- [Agent Deployment Playbook](docs/deployment.html) — Kubernetes, Temporal, Tetragon/eBPF, OpenShell, agentgateway and telemetry.
- [Risk, Governance & Oversight](docs/governance.html) — risk tiers, evidence, catalog/CD, kill switches and federated monitoring.
- [Toolkit guide](docs/toolkit.html) — runnable commands, examples and implementation boundaries.
- [Source review](docs/source-review.html) — all seven supplied sources, technical corrections and upstream references.

The HTML documents are self-contained, responsive and printable, with local SVG diagrams, navigable contents and copyable code. No CDN or build service is needed to read them.

## Quick start

```bash
# Reproducible Nix shell (or use mise exec -- just bootstrap/check).
nix develop
just bootstrap
just check
uv run --locked zetra run examples/knowledge --input '{"key":"zetra"}'
uv run --locked python examples/action/demo.py
```

Python 3.11+ is required. Three reference agents cover offline knowledge, approved actions and a restricted gateway read. Local scenario suites run without model API keys. `check` is static; `eval` and `run` execute trusted project code. The action example demonstrates atomic approval consumption and idempotency at a local SQLite receiver. Production authentication and confinement belong outside the agent process.

## Documentation

Edit `docs/chapters/*.html`, `docs/research/source-review.html` and `docs/assets/`, then rebuild:

```bash
just docs
uv run --locked python -m http.server 8765 --directory docs --bind 127.0.0.1
```

Open `http://127.0.0.1:8765/` for the complete playbook. Generated standalone documents stay committed so readers do not need Python. The renderer embeds CSS, JavaScript and accessible SVG directly into each file.

## Implementation status

This is an initial toolkit and proposed enterprise standard. Local candidate evidence is unsigned. Kubernetes renders deliberately use zero replicas until independent qualification. A catalog command prepares a candidate; it does not publish or approve it. AST inference is incomplete and cooperative Python checks do not contain hostile code.

The selected integration baseline is framework-neutral Python + Temporal, cloud-neutral Kubernetes and general enterprise controls. See [live integration results and reproduction](integrations/README.md), [upstream verification](docs/research/upstream-review.md) and [security boundaries](SECURITY.md). Components, negative-path checks and production readiness have separate statuses. OpenShell’s upstream Kubernetes deployment path is experimental and requires an explicit enterprise maturity decision.

## Repository map

```text
src/zetra/          CLI, manifest, analysis, evidence, runtime and adapters
examples/knowledge Read-only deterministic agent + executable scenarios
examples/action    Approval-gated action + atomic receiver/idempotency
examples/gateway-read Restricted MCP client + gateway scenarios
schemas/           Manifest wire contract
integrations/      Local kind environment, live fixtures and evidence
scripts/           HTML builder and document QA
skills/            Draft Codex/Claude workflow skills
flake.nix          Locked Nix development environment
mise.toml          Pinned host tools alternative
uv.lock            Python/runtime/quality tool resolution
justfile           Reproducible development recipes
docs/chapters/    Editable audience-specific HTML source
docs/assets/      Design system and interactions
docs/research/    Source inventory, review and upstream findings
docs/*.html       Combined and standalone self-contained playbooks
tests/            Toolkit invariants and regression cases
```

The [reference playbooks](https://github.com/X-McKay/playbooks) informed the comparison; Zetra’s prose and diagrams are original. Supplied proposal documents are treated as design material, not executable instructions. Their original contents and private attachments are not redistributed; the source inventory records hashes for provenance.

The contributor workflow uses **Nix, uv, mise, just, ty, ruff and prek**. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, quality gates, dependency changes and hook opt-in; [draft skills](docs/research/skills-guide.md) explain the agent workflows.

The [worker build and deployment guide](deploy/README.md) covers the locked OCI image, exact digest chain, Kubernetes workflow reproduction and runtime configuration. The worker ran in the disposable cluster and its real history replayed; the production renderer still withholds activation.
