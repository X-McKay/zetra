# Agent registry

The record of what agents, tool servers and model engines exist, which releases of them are
deployed where, who approved what, and what each cluster reports. One service
(`agent-registry`), PostgreSQL behind it, and an HTTP interface. A cluster's provisioner
(`platform/provisioner/`) asks it what to apply and reports back what it sees.

Milestone 1 covers one cluster, one tenant, and agents that are either owned by the pull
request path (imported, read-only here) or by the registry (released, approved and projected
from here).

## Where things are

| Path | What |
| --- | --- |
| `contract/import.md` | the catalog feed and how agents of the pull request path are indexed |
| `contract/lifecycle.md` | recording a release, the verdict, approval, activation, pin, enable |
| `contract/projection.md` | what a cluster is given, what it reports, authority, recovery |
| `contract/openapi.json` | the HTTP interface, generated; a test fails when it is out of date |
| `contract/gate-cases.json`, `contract/authority-cases.json` | the approval gates and the "asks for more" rule, as cases |
| `contract/examples/` | one answer of the desired route and one observation report |
| `agent_registry/models.py`, `store/` | the records and the store protocol; `store/memory.py` for tests, `store/pg.py` for PostgreSQL |
| `agent_registry/canonical.py`, `defaults.py`, `derive.py` | documents (an Agent with its defaults filled in), digests and derived records, shared by everything |
| `agent_registry/rules/` | the decisions, each a function of the store |
| `agent_registry/api/` | routes, callers and the error body |
| `migrations/` | numbered SQL, applied at start |

## Checks

```bash
uv sync --frozen
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

Tests marked `postgres` need a database and are skipped without one:

```bash
REGISTRY_TEST_DSN=postgresql://registry@localhost:5432/registry uv run pytest -q
```

After a change to a route or a schema:

```bash
uv run agent-registry openapi --write contract/openapi.json
```

## Tests

`tests/store_suite.py` is run against every store. `tests/world.py` is a small registry
holding the `db-admin` fixtures, with a clock and a cluster that the test controls;
`tests/test_write_cases.py` is the lifecycle contract as tests through the HTTP application.
