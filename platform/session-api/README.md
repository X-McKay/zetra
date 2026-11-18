# session-api

The session interface of `contract/sessions.md`: starts, reads, cancels and lists the sessions
of one tenant's agents. One instance per tenant, beside that tenant's session runner. It uses
the runner's own code for Temporal (`runner.submit`, `runner.worker.connect`,
`runner.operator.read_report`), so the workflow id, queue, codec and input rules are not
written twice. It decides nothing about which release is current or approved.

Every decision is in `session_api/service.py`. `session_api/ports.py` holds the three seams
(`Sessions`, `Objects`, `Reports`); `session_api/real.py` wraps Temporal, Kubernetes and the
artifact store and is not run by the tests. Each start and cancel writes one JSON line to
standard output (`session_api/record.py`): never the task, the report or a token.

## Settings (environment)

| Variable | |
| --- | --- |
| `SESSION_API_TENANT` | required: the one tenant served |
| `SESSION_API_CLUSTER` | required: the cluster's name, as the registry knows it |
| `SESSION_API_ARTIFACT_ROOT` | required: the runner's artifact store, read only |
| `SESSION_API_ISSUER` | default `https://idp.almckay.io/realms/agents` |
| `SESSION_API_AUDIENCE` | default `agent-registry` |
| `SESSION_API_JWKS_URL` | default `<issuer>/protocol/openid-connect/certs` |
| `SESSION_API_WORKER_POOL` | `legacy` (default) or `governed-v1` |
| `SESSION_API_HOST`, `SESSION_API_PORT` | default `0.0.0.0`, `8080` |

The Temporal settings are the runner's (`TEMPORAL_*`, `PAYLOAD_KEY_FILE`).

## Checks

```
uv sync --frozen
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```

The image builds from `platform/`: `docker build -f session-api/Dockerfile platform/`.
