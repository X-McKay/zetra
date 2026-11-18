# registry-provisioner

A small program that runs in a cluster. About every 30 seconds it asks the agent registry
which `Agent` and `AgentRelease` objects this cluster should hold, applies them with
server-side apply under the rules of `platform/registry/contract/projection.md` (sections 2, 4,
5 and 6), and reports what it sees. It never deletes anything and writes only `Agent` and
`AgentRelease` in the namespaces the registry lists, plus its own ConfigMap
`registry-provisioner-state` (key `lastGeneration`).

URLs must be `https`, or `http` only to a host ending in `.svc` or `.svc.cluster.local`. If
an apply returns an object that still differs from the desired one (another field manager owns
a field the registry's document lacks), the result is `error`, not a repair on every pass.

It gets a Keycloak access token by presenting its projected service-account token as a client
assertion (no client secret), re-reading the token file at every refresh.

## Settings (environment)

| Variable | |
| --- | --- |
| `PROVISIONER_CLUSTER` | required: the cluster name |
| `PROVISIONER_REGISTRY_URL` | required: `https://` base URL of the registry |
| `PROVISIONER_NAMESPACE` | required: its own namespace, for the state ConfigMap |
| `PROVISIONER_TOKEN_URL` | the realm's token endpoint (`https://`) |
| `PROVISIONER_TOKEN_PATH` | the projected service-account token file |
| `PROVISIONER_CLIENT_ID` | default `registry-provisioner-<cluster>` |
| `PROVISIONER_INTERVAL_SECONDS` | default 30 |
| `PROVISIONER_CA_FILE` | optional CA bundle for both HTTPS calls |
| `PROVISIONER_HEARTBEAT_FILE` | optional; touched at the end of every pass, whatever its result |

`registry-provisioner --once` runs one pass: exit 0 when the registry answered and accepted
the report, 1 otherwise. Without `--once` it loops until SIGTERM and never exits on a failed
pass.

## Checks

```
uv sync --frozen
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```
