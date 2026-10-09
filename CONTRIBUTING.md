# Contributing to Zetra

Zetra treats an agent as a governed software package. Changes must preserve the separation between developer intent, candidate artifacts, live runtime qualification and production approval. A simulated event, discovery probe or passing unit test does not establish runtime enforcement.

## Start with one reproducible environment

Choose either Nix or mise. Both lead to the same `just` recipes and committed `uv.lock`; using both simultaneously is unnecessary.

**Nix (Apple Silicon macOS, ARM64 Linux, x86-64 Linux):**

```sh
nix develop
just bootstrap
just check
```

`flake.lock` pins Nixpkgs by commit and content hash. The development shell provides Python, uv, just, Git, mise, nixfmt, actionlint and shellcheck. Python application dependencies, Ruff, ty, prek and type stubs come from `uv.lock`, rather than the versions bundled in Nixpkgs. The current Nixpkgs input has dropped Intel macOS support; use mise on that platform. We do not change Nix channels or global configuration.

**mise:**

```sh
mise trust mise.toml
mise install
mise exec -- just bootstrap
mise exec -- just check
```

Read the configuration before trusting it: mise configuration can execute commands. The file pins Python 3.12.13, uv 0.12.7, just 1.58.0, actionlint 1.7.12 and shellcheck 0.11.0. `mise exec` avoids requiring global shell activation. Tools may be downloaded to mise's managed installation directory; no project setup modifies global version defaults.

**Existing tools:** run `just bootstrap` with the pinned Python and uv versions available. `.python-version` selects the default development interpreter; the package supports Python 3.11 and later. CI checks the minimum supported Python and the primary development version. `ty` analyzes contracts against Python 3.11, and Ruff applies compatible modernization rules.

`uv sync --locked --all-extras` creates the local `.venv`, includes the Temporal extra and development dependencies, and fails if the lockfile is stale. Cache files live in ignored `.tools/` when using just or mise. Use `uv run --locked --all-extras` for direct commands requiring the Temporal SDK. `uv run` makes the minimum changes needed by default; `uv sync` performs an exact sync and removes packages outside the selected dependencies. Omitting extras from a later exact sync can remove Temporal. The project requires uv 0.12.7 or a compatible 0.12 release.

## Daily workflow

```sh
just format        # safe Ruff fixes and Python formatting
just lint          # lint and formatter checks without writes
just typecheck     # ty, including package templates and examples
just ci-lint       # GitHub Actions expressions and embedded shell
just shell-lint    # ShellCheck of the integration launcher
just test          # deterministic unit and contract tests
just docs          # rebuild and validate HTML playbooks
just examples      # evaluate reference agents and test the offline developer starter
just check         # complete local contributor gate
```

Change source chapters in `docs/chapters/`, the implementation, schemas or tests. Regenerate the combined book and standalone documents with `just docs`; include generated artifacts in a review. Update examples and scaffolding templates together when their public contracts change. Use stable scenario IDs and deterministic assertions, including forbidden paths and failure cases. Prefer tests of security invariants and observed behavior over tests that mirror private implementation details.

The fast gate has no live model keys or cluster credentials. Example evaluation uses controlled reference implementations; it does not measure a production model's quality. Passing it does not authorize a deployment. Live model evaluations and runtime qualification require explicit target versions, approved connectivity and an evidence report that states its scope.

## Git hooks with prek

The repository uses compatible `.pre-commit-config.yaml` local hooks. Hook commands execute the already locked uv environment; there are no remotely fetched hook repositories or independent tool version pins.

```sh
just hooks-config  # validate configuration without installing hooks
just hooks-check   # run on Git-tracked files
```

Hooks are not installed automatically. After reviewing the configuration, a contributor may explicitly opt in within their checkout:

```sh
uv run --locked prek install
# To remove the checkout's hook shim:
uv run --locked prek uninstall
```

`--all-files` selects tracked files: untracked new files require staging or explicit `prek run --files path/to/file.py`. There is no blanket lint/type suppression. Fix the contract, add accurate types or narrow a value before use. A necessary suppression must be local, explained and reviewable.

## Change dependencies and host tools deliberately

```sh
uv add package-name
uv add --dev development-tool
uv lock --check
just check
```

Commit `pyproject.toml` and `uv.lock` together. `uv.lock` is the authoritative Python resolution, including hashes and supported platform wheels; the previous pip snapshot is retired. For scheduled dependency maintenance, `just upgrade` resolves newer compatible versions and installs them. Review transitive changes and rerun tests before merging.

Update mise's exact host tool pins deliberately. Update Nix with `nix flake update`, review `flake.lock`, then run `just nix-check` and enter the shell. Nixpkgs and mise may provide different host tool patch versions; application/tool behavior is stabilized by the uv lock. Do not claim identical complete environments across these two alternatives.

```sh
just nix-check                     # evaluates all declared shells; no builds
nix develop --command just check   # realizes the host shell and runs the gate
nix fmt                            # format flake.nix with its locked formatter
```

CI pins external GitHub Actions to immutable commits, runs locked Python checks and example evaluations on ephemeral Linux runners, and separately evaluates/realizes the Nix shell. Workflow execution on GitHub has not occurred merely because local commands pass. Local validation details are recorded in [the tooling report](docs/research/tooling-validation.json).

## Runtime and deployment changes

Every adapter must document its supported upstream versions, policy semantics, prerequisites and unsupported mandatory controls. Fail closed when mandatory enforcement cannot be established. For Temporal changes, replay representative histories and review worker compatibility, activity idempotency, cancellation and retries. Local retry/replay fixtures are a first check; production history replay remains a release responsibility.

For security changes, run allowed and forbidden paths on the exact supported runtime and record observable outcomes in an integration report. Demonstrate authentication failure, authorization denial, direct egress denial and relevant sandbox restrictions. Bind approvals to agent identity, artifact digest, policy version, tool and normalized arguments when applicable. Do not turn an assertion in developer configuration into evidence that a live control was installed.

Use a dedicated kubeconfig and explicit context for disposable validation clusters. Review [the local cluster notes](integrations/LOCAL-CLUSTER.md) and [the gateway integration runbook](integrations/gateway-LIVE-VALIDATION.md) before running integration commands. `just check` never selects or mutates a shared Kubernetes cluster. Do not commit secrets, kubeconfigs, tool binaries, raw user attachments, private signing material or sensitive telemetry. Keep sensitive temporary reports under `integrations/results/private/` or `.tools/`.

A review should explain the user-visible behavior, tests performed, exact integration scope, remaining unknowns and residual risks. Update the playbook whenever a claim about enforcement or operational capability changes.

## Optional browser layout checks

`scripts/visual_qa.cjs` opens every generated HTML document at desktop and mobile sizes, checks browser errors and horizontal overflow, exercises navigation and the boundary diagram, and writes ignored screenshots to `build/doc-qa/` plus a small report to `docs/research/document-qa.json`. It requires Node 20+, Playwright 1.62.1 and an installed Chrome. Install the optional QA library locally, rather than changing global packages:

```bash
npm install --prefix .tools/browser-qa --save-exact playwright@1.62.1
ZETRA_PLAYWRIGHT_PATH="$PWD/.tools/browser-qa/node_modules/playwright" \
  node scripts/visual_qa.cjs
```

Set `ZETRA_CHROME_PATH` to an explicit browser executable if Chrome's normal channel discovery does not locate it. This is an optional visual check; the standard `just docs` gate uses Python only. Inspect screenshots after a design change. `just evidence-index` refreshes the index of already recorded live reports; it does not rerun their probes or establish fresh production evidence. Live `just lab-*` recipes mutate only their documented disposable fixtures and require the integration prerequisites and explicit local kubeconfig described in `integrations/README.md`.
