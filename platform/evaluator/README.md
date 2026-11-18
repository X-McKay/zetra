# registry-evaluator

A small program that runs in a cluster, one per cluster. It claims the oldest evaluation the
agent registry holds for the cluster, runs the release's evaluation suite one case at a time
through the session interface (`platform/session-api/contract/sessions.md`), and posts the
verdict. The contract is `platform/registry/contract/delivery.md`, sections 1.3 and 6.

A case is a session started with the release named and `purpose: "evaluation"`. Its id is a
version 4 UUID made from the sha256 of `<deployment>@<revision>#<attempt>/<case id>`, so a
second evaluator, or this one after a restart, finds the session that exists and starts none.
The case's `task` is sent as it is written. A case passes when the session ends `completed` and
every check of `expect` holds on the report: a string report is checked as it is, any other
JSON value as its compact JSON text, and a missing report as the empty string.

Verdicts: `passed` when the cases that passed meet `acceptance.min_passed`; `failed` when every
case ran and they do not; `error` when a start is refused (4xx), the interface is unreachable
after 5 tries over about a minute, or a case runs past `timeout_seconds` (the session is then
cancelled). A result the registry did not accept (5xx or network error) is posted again on the
next pass; the cases are not run again.

It handles no grant handle and logs no token. It logs one line per evaluation.

## Settings (environment)

| Variable | |
| --- | --- |
| `EVALUATOR_CLUSTER` | required: the cluster name |
| `EVALUATOR_REGISTRY_URL` | required: `https://` base URL of the registry |
| `EVALUATOR_SESSION_API_URL` | required: template with `{tenant}`, e.g. `http://session-api-{tenant}.agent-platform.svc:8080` |
| `EVALUATOR_NAME` | this evaluator's name; default the host name |
| `EVALUATOR_POLL_SECONDS` | wait when there is nothing to do; default 15 |
| `EVALUATOR_TOKEN_URL` | the realm's token endpoint (`https://`) |
| `EVALUATOR_TOKEN_PATH` | the projected service-account token file |
| `EVALUATOR_CLIENT_ID` | default `registry-evaluator-<cluster>` |
| `EVALUATOR_CA_FILE` | optional CA bundle for the HTTPS calls |

URLs must be `https`, or `http` only to a host ending in `.svc` or `.svc.cluster.local`. The
same access token goes to the registry and to the session interface.
