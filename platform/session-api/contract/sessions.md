# The session interface (milestone 2)

Starts a session of an agent's release, reads it, cancels it, and lists and cancels the running
sessions of one release. It replaces `kubectl exec` into the session runner for agents the
registry owns. It is the only way the evaluator and the provisioner touch sessions.

It does not decide which release is current, approve anything, or handle a person's consent.

## 1. Shape

One instance per tenant, as a second container beside that tenant's session runner. It uses
the runner's own code to talk to Temporal (`runner.worker.connect`, `runner.submit.submit`,
`runner.types.SessionInput`, `runner.operator.read_report`), so the workflow id, the queue,
the payload encryption and the input rules are the runner's and are not written twice.

Package `platform/session-api/`, Python package `session_api`, the same web framework and
token library as the registry. The runner is a path dependency; the image is built from the
`platform` directory, as the controller's is.

Settings, from the environment:

| Variable | Meaning | Default |
| --- | --- | --- |
| `SESSION_API_TENANT` | The one tenant this instance serves | required |
| `SESSION_API_CLUSTER` | The cluster's name, as the registry knows it | required |
| `SESSION_API_ISSUER` | Token issuer | `https://idp.almckay.io/realms/agents` |
| `SESSION_API_AUDIENCE` | Token audience | `agent-registry` |
| `SESSION_API_JWKS_URL` | Signing keys | the issuer's certificate address |
| `SESSION_API_ARTIFACT_ROOT` | The runner's artifact store, read only | required |
| `SESSION_API_WORKER_POOL` | The runner pool sessions are queued for | `legacy` |
| `SESSION_API_HOST`, `SESSION_API_PORT` | Listen address | `0.0.0.0`, `8080` |

The Temporal variables are the runner's (`TEMPORAL_*`, `PAYLOAD_KEY_FILE`).

The audience is the registry's on purpose: one sign-in serves `just agent` for both. The
roles below keep the two apart.

## 2. Callers

Tokens are verified as the registry verifies them (`agent_registry/api/jwks.py` is the
pattern; the code is copied, not imported). Roles are realm roles.

| Caller | Recognised by | May |
| --- | --- | --- |
| Operator | Role `registry-operator`, a person, with an email address | Start on the current release; read; cancel; list; cancel by release |
| Evaluator | Role `registry-evaluator`, a service account whose `azp` is `registry-evaluator-<SESSION_API_CLUSTER>` | Start on any Ready release of the agent, with purpose `evaluation`; read and cancel sessions whose purpose is `evaluation` |
| Provisioner | Role `registry-provisioner`, a service account whose `azp` is `registry-provisioner-<SESSION_API_CLUSTER>` | List; cancel by release |

Anything else is refused. No route accepts a grant handle and none returns one.

## 3. Errors

The registry's body and codes: `{"error": {"code": ..., "message": ..., "retryable": ...}}`.

| Code | Status | When |
| --- | --- | --- |
| `invalid-arguments` | 400 | The body or a parameter breaks a rule below or a rule of `SessionInput.validate` |
| `not-signed-in` | 401 | No token, or one that does not verify |
| `refused` | 403 | The token is valid and the caller may not do this |
| `not-found` | 404 | Another tenant in the path; no such Agent, AgentRelease or session |
| `refused` | 409 | A rule says no; the message says which |
| `wrong-path` | 409 | The agent is not owned by the registry |
| `conflict` | 409 | The session id exists with other input |
| `unavailable` | 503 | Temporal or the Kubernetes API could not be reached; retryable |

## 4. Routes

Every path starts `/v1/tenants/{tenant}`. A `tenant` other than `SESSION_API_TENANT` is
`not-found` before anything else is checked. `GET /healthz` needs no token and answers 200.

### 4.1 Start

`POST /v1/tenants/{tenant}/sessions`

```json
{"agent": "db-admin", "task": {"prompt": "..."}, "release": null, "sessionId": null, "purpose": "task"}
```

`task` is any JSON value the runner accepts (at most 64 KiB, no credentials). `sessionId`,
when given, is a lower-case version 4 UUID; otherwise one is made. `purpose` is `task` or
`evaluation`.

Checks, in this order. The first that fails answers.

| # | Check | Otherwise |
| --- | --- | --- |
| 1 | The caller is an operator or the evaluator | 403 `refused` |
| 2 | An operator's purpose is `task`; the evaluator's is `evaluation` | 403 `refused` |
| 3 | The Agent exists in the tenant's namespace | `not-found` |
| 4 | The Agent carries the annotation `registry.agentplatform.io/deployment` | `wrong-path`: "db-admin is not owned by the registry; use the pull request path's run command" |
| 5 | `spec.enabled` is not false | `refused`: "the agent is disabled" |
| 6 | The release is named, or the Agent carries `registry.agentplatform.io/current-release` | `refused`: "the agent has no current release" |
| 7 | The release is the current one, or the caller is the evaluator | 403 `refused`: "only the evaluator may name a release that is not current" |
| 8 | The AgentRelease exists in the namespace and its `spec.agent` is the agent | `not-found` |
| 9 | Its `Ready` condition is True | `refused`: "release R is not Ready: <reason>: <message>" |
| 10 | The identity mode is `agent`. It is read from the release's `spec.declaration` when it has one, otherwise from the Agent. | `refused`: "this agent acts for a person and needs that person's consent; it cannot be started here" |

Then the session is submitted with `grant_handle` none, the pool from the settings, and a
Temporal memo `{"release": ..., "startedBy": <the caller's name>, "purpose": ...}`.
`runner.submit.submit` gains an optional `memo` argument for this and is otherwise unchanged.

Answer, 202:

```json
{"sessionId": "...", "agent": "db-admin", "release": "db-admin-2026-11-20-1a2b3c4d",
 "workflowId": "session:tenant-test:db-admin:...", "state": "running", "created": true}
```

A `sessionId` that already exists with the same agent, release and task answers 200 with
`created: false` and starts nothing. With other input it is `conflict`.

### 4.2 Read

`GET /v1/tenants/{tenant}/sessions/{agent}/{sessionId}`. Operator; the evaluator for sessions
whose purpose is `evaluation`.

```json
{"sessionId": "...", "agent": "db-admin", "release": "...", "purpose": "task",
 "startedBy": "al@example.com", "state": "finished", "outcome": "completed",
 "startedAt": "...", "finishedAt": "...",
 "progress": null, "report": "...", "reportProblem": null}
```

| Temporal says | `state` | `outcome` |
| --- | --- | --- |
| Running | `running` | null |
| Completed, result outcome `COMPLETED` | `finished` | `completed` |
| Completed, result outcome `FAILED` | `finished` | `failed` |
| Completed, result outcome `TIMED_OUT` | `finished` | `timed-out` |
| Completed, result outcome `NOT_CONFIRMED` | `finished` | `not-confirmed` |
| Cancelled | `finished` | `cancelled` |
| Failed, terminated or timed out | `finished` | `failed` |

`progress` is the answer of the workflow's `status` query while running, and null when the
query fails or the session has finished.

`report` is the session's report (`runner.operator.read_report` on the result's snapshot) when
the session finished with a snapshot. When there is none or it cannot be read, `report` is
null and `reportProblem` says why in one sentence. A missing report is not an error of the
route.

`release`, `purpose` and `startedBy` come from the memo. For a session with no memo (one
started another way), `release` is read from the workflow's input, `purpose` is `task` and
`startedBy` is null.

### 4.3 Cancel one

`POST /v1/tenants/{tenant}/sessions/{agent}/{sessionId}/cancel` with `{"reason": "..."}`
(1 to 500 characters). Operator; the evaluator for sessions whose purpose is `evaluation`.

Asks Temporal to cancel the workflow. The workflow cleans up and closes as cancelled; this
route does not wait for that. Answer 200 `{"sessionId": "...", "cancelRequested": true}`, or
`cancelRequested: false` when the session had already finished.

### 4.4 List

`GET /v1/tenants/{tenant}/sessions?state=running&agent=<name>&release=<name>`. Operator;
provisioner. `state` must be `running`. `agent` and `release` are optional.

```json
{"sessions": [{"sessionId": "...", "agent": "db-admin", "release": "...", "purpose": "task",
               "startedBy": "...", "startedAt": "..."}]}
```

It lists the running `SessionWorkflow` executions of the tenant's Temporal namespace and
filters them here. The release of a session with no memo is read from the workflow's input, so
that a session started another way is still found by its release.

### 4.5 Cancel by release

`POST /v1/tenants/{tenant}/sessions/cancel` with
`{"agent": "db-admin", "release": "...", "reason": "..."}`. Operator; provisioner.

Cancels every running session of that agent and release, including those with no memo.

```json
{"cancelled": ["<sessionId>", "..."], "failed": [{"sessionId": "...", "message": "..."}]}
```

It answers 200 when the list could be read, even if some cancellations failed. Calling it
again is safe: a session already cancelling is asked again and nothing else happens. It does
not stop new sessions; that is the registry's part (the release is no longer Ready, so check 9
refuses).

## 5. Record

Every start, cancel and cancel by release writes one JSON line to standard output: the time,
the route, the caller's name and role, the agent, the release, the session id, the purpose and
the answer's code. Never the task, the report or a token.

## 6. Seams for tests

Three small interfaces, each with a fake:

- `Sessions`: start, describe, query, read input, cancel, list running. The real one wraps a
  Temporal client.
- `Objects`: read one Agent, read one AgentRelease. The real one wraps the Kubernetes client.
- `Reports`: read the report of a snapshot. The real one wraps the artifact store.

Tests are hermetic: no Temporal server, no cluster, no network.

Tests must show:

1. A session is started and read back, with the memo that was set.
2. No token is `not-signed-in`; a reader's token is refused; a token for another cluster's
   evaluator or provisioner is refused.
3. An operator naming a release that is not current is refused; the evaluator is not.
4. Each of checks 3 to 6 and 8 to 10, one test each, and that nothing was submitted.
5. The same `sessionId` twice starts one session; with another task it is `conflict`.
6. Each row of the table in 4.2.
7. Cancel by release cancels a session with no memo whose input names the release, and
   leaves a session of another release alone.
8. The evaluator cannot read or cancel a session whose purpose is `task`.
9. The record of a start has no task text in it.
10. `submit` with a memo passes it to Temporal; without one it behaves as before (in the
    runner's tests).

## 7. What the cluster repository provides

Listed so that nothing is assumed (package 2.10):

- The container in each tenant's session runner Deployment, with the runner's Temporal
  certificate, payload key and artifact volume mounted read only.
- Read access to Agents and AgentReleases in the tenant's namespace for its service account.
- The Temporal proxy's allowlist covering start, describe, query, cancel, list and reading a
  workflow's history for that client.
- A route for `/v1/tenants/<tenant>/sessions` on the registry's host name, and a Service the
  evaluator and the provisioner reach inside the cluster
  (`http://session-api-<tenant>.agent-platform.svc:8080`).
- Network policy for those three callers and for the instance's own calls to the identity
  provider's keys, the Kubernetes API and Temporal.
