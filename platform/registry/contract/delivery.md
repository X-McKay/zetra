# Delivery: from a merge to a current release (milestone 2)

This file fixes what milestone 2 builds. It adds to `lifecycle.md` and `projection.md`. Where
it differs from them, this file is right and they are updated by the package that changes the
code (section 13 names which).

One agent is on this path in milestone 2: `db-admin`. Every other agent keeps the pull request
path and must behave exactly as before.

Terms:

- **Candidate**: the newest revision of a deployment while its phase is `awaiting-runtime`,
  `pending-approval` or `evaluating`.
- **Current**: the revision `Deployment.current_revision` names. New sessions start on its
  release.
- **Carried declaration**: `AgentRelease.spec.declaration` (section 2).
- **Agent in force**: the Agent document the cluster is given (section 4).

## 1. What the author writes

### 1.1 One file

An agent that has a `requires` block in `agent.yaml` is on the one-file layout. The author
edits `agent.yaml` and nothing under `deploy/`.

```yaml
schema_version: 1
name: db-admin
entrypoint: db_admin.agent:create_agent
instructions: |
  ...
model:
  backend: llm-main
  name: main            # a model alias (1.2)
  max_tokens: 1024
  temperature: 0
tools: [...]            # generated from the code, as today
requires:
  owner: al
  description: Read-only PostgreSQL administrator.
  tenant: tenant-test
  identity: {mode: agent}
  budget: {tokensPerDay: 1000000, tokensPerSession: 200000}
  tools:
    - backend: postgres-admin
      effect: remote_read
      tools: [execute_sql]
      delegation: inherit
  runtime: {cpu: "1", memory: 2Gi, ttlSeconds: 900}
```

`deploy/agent.yaml` is generated from it and committed. Its first line is a comment saying
which command wrote it and that it is not edited by hand. The mapping:

| Agent field | From |
| --- | --- |
| `metadata.name` | `name` |
| `metadata.namespace` | `requires.tenant` |
| `spec.owner`, `spec.description`, `spec.identity`, `spec.runtime` | the same keys of `requires` |
| `spec.capabilities.models` | one entry: `backend` and `models: [name]` from `model`, `budget` from `requires.budget`. Empty list when `model` is null. |
| `spec.capabilities.mcp` | `requires.tools`, unchanged |

Keys of `requires` are checked: an unknown key is an error that names it. The generated Agent
is checked against the Agent schema as today.

The command that writes the manifest's `tools` block today also writes `deploy/agent.yaml`.
`check` fails when either is out of date and names that command.

An agent with no `requires` block is on the two-file layout. Nothing about it changes. The
frozen fixtures stay on it.

The runtime reads `entrypoint`, `instructions`, `tools` and `model` from the manifest in the
image. Package 2.7 proves with a test, using the runtime's own loader, that a manifest with a
`requires` block loads. If it does not, 2.7 stops and reports; it does not change the runtime.

### 1.2 The model alias

On the one-file layout `model.name` must be an alias, and the alias must belong to
`model.backend`. The table of aliases is built in:

| Alias | Backend |
| --- | --- |
| `main` | `llm-main` |
| `fast` | `llm-fast` |

A repository may replace the table in its root `pyproject.toml`:

```toml
[tool.zetra-agents.aliases]
main = "llm-main"
fast = "llm-fast"
```

`check` on an unknown alias fails with the file, the line of `model.name`, the name found and
the aliases it may be. A concrete model name (anything not in the table) is the same error.

The agent sends the alias as the model name. The declaration grants the alias on the backend.
The gateway backend pins the model that serves it. What must be true on the cluster for this
to work is in section 12.

On the two-file layout `model.name` is checked as today.

### 1.3 The evaluation suite

`agents/<name>/evaluation/suite.yaml`, optional:

```yaml
schema_version: 1
timeout_seconds: 600          # for one case; 30 to 3600
acceptance:
  min_passed: all             # "all", or a whole number from 1 to the number of cases
cases:
  - id: instance-summary      # a name (lowercase letters, digits, hyphens), unique in the file
    task: Report the server version and the number of databases.
    expect:
      outcome: completed      # the only value in milestone 2; the default
      report_contains: ["PostgreSQL"]        # every string is in the report
      report_matches: ["databases?:\\s+\\d+"] # every pattern is found (Python `re.search`)
      report_excludes: ["Traceback"]         # no string is in the report
```

Rules: 1 to 20 cases; a task is a string of 1 to 4000 characters; each list has at most 20 entries of at
most 500 characters; a pattern must compile; an unknown key anywhere is an error. `check`
applies these rules and so does the registry when a release is reported;
`suite-cases.json` holds suites both must accept and suites both must refuse. Scoring is these
checks and nothing else in milestone 2: no model judges an answer.

## 2. The carried declaration

`AgentRelease.spec.declaration` is the Agent's `spec` as it was when the release was built,
without `enabled`. It is optional and cannot be changed once set.

| Who | Does what |
| --- | --- |
| Tooling (`release.build`) | Writes it into `agentrelease.yaml` for every agent, from `deploy/agent.yaml`. The compiled policy is unchanged, so `policySha256` and the attestation are unchanged. |
| CRD | `spec.declaration` has the schema of the Agent's `spec` without `enabled`. A rule refuses a change: `!has(oldSelf.declaration) || (has(self.declaration) && self.declaration == oldSelf.declaration)`. A test holds the two schemas equal. |
| Registry | Keeps it as part of the release document and gives it to the cluster unchanged. Refuses a reported release whose declaration is not equal to the reported Agent's `spec` without `enabled` (`invalid-arguments`). |
| Controller | When it is present, compiles the release's policy and its pod template from an Agent made of the live Agent's `metadata` and the carried declaration as `spec`. `PolicyDrift` compares that result with `policySha256`, exactly as today. `enabled`, the Agent's readiness and the identity provider objects still come from the live Agent. When it is absent, everything is as today. |

The result: two releases of one agent whose declarations differ are both ready at once. A
release without a carried declaration still drifts when the live Agent changes, as today.

The CRD must be on the cluster before the first release that carries a declaration is
projected. The API server drops a field it has no schema for, without an error.

## 3. The release report

The release workflow tells the registry what it published. The report is a claim. Nothing is
approved or made current because of it: the controller verifies the signature and the
attestations on the projected object, and the provisioner reports that.

### 3.1 Route

`POST /v1/reports/releases`

```json
{
  "agent": {"apiVersion": "agentplatform.io/v1alpha1", "kind": "Agent", "...": "..."},
  "agentRelease": {"apiVersion": "agentplatform.io/v1alpha1", "kind": "AgentRelease", "...": "..."},
  "lineage": {"checksums": {"agent.yaml": "sha256:...", "agentrelease.yaml": "sha256:..."}},
  "evaluation": {"suite": {"schema_version": 1, "...": "..."}}
}
```

`evaluation` is optional. `agent` and `agentRelease` are the published files as JSON.

Answer, 200: `{"deployment": "...", "release": "...", "revision": 3, "created": true}`. The
same report again answers the same with `created: false` and writes nothing.

### 3.2 Who may send it

The caller presents a GitHub Actions identity token as a bearer token. No Keycloak token is
accepted on this route, and this token is accepted on no other route.

| Check | Refusal |
| --- | --- |
| The signature verifies against the keys of `REGISTRY_GITHUB_ISSUER` (default `https://token.actions.githubusercontent.com`), `iss` is that issuer, `aud` is `REGISTRY_REPORT_AUDIENCE`, and the token has not expired | 401 `not-signed-in` |
| `REGISTRY_REPORT_AUDIENCE` is set | 404 `not-found` (reports are off) |
| `ref_type` is `tag` and `ref` is `refs/tags/<agent>/v<version>` | 403 `refused` |
| A trusted repository exists whose `repository` equals the claim `repository`, whose `agents` contains `<agent>`, and whose `subject_reg_exp` fully matches `"https://github.com/" + job_workflow_ref` | 403 `refused` |
| `agent.metadata.name` and `agentRelease.spec.agent` are `<agent>` | 400 `invalid-arguments` |
| `agent.metadata.namespace` is the namespace of that trusted repository's tenant | 400 `invalid-arguments` |

When more than one trusted repository matches, the one whose tenant's namespace equals
`agent.metadata.namespace` is used.

The deployment is `agent:<cluster>:<tenant>:<agent>` of that trusted repository. Then the
checks of `lifecycle.md` section 4 apply with these outcomes:

| Case | Answer |
| --- | --- |
| No such deployment | 404 `not-found`. Creating the first deployment from a report is not in milestone 2. |
| The deployment is on the pull request path | 409 `wrong-path`. The workflow treats this as "not reported" and succeeds. |
| The registry is in recovery | 409 `in-recovery`. The workflow fails; a person runs it again. |
| The release is revoked (section 8) | 409 `refused` |

Then `lifecycle.record_release` runs with these additions:

- `lineage` is stored as `{repository, tag, commit, workflow, checksums, evaluation}`.
  `repository`, `tag`, `commit` and `workflow` come from the token (`repository`, `ref`
  without `refs/tags/`, `sha`, `job_workflow_ref`), never from the body.
- A suite that breaks a rule of 1.3 is 400 `invalid-arguments` and nothing is recorded.
- The declaration check of section 2.
- The actor of the event is `github:<repository>@<tag>`.

The existing route `POST /v1/deployments/{id}/releases` stays for a signed-in operator and
gains the same declaration check and an optional `evaluation`.

### 3.3 The workflow side

The last step of `.github/actions/agent-release` sends the report when the input
`registry-url` is not empty. It asks GitHub for a token with the audience given by the input
`registry-audience`, builds the body from the files it published and the suite file if there
is one, and posts it. It retries 5 times over about a minute on a network error or a 5xx.

| Answer | Step result |
| --- | --- |
| 200 | Succeeds; prints deployment, release and revision |
| 409 `wrong-path` | Succeeds; prints that the agent is on the pull request path |
| Anything else | Fails, printing the code and message. The release itself is already published and is not withdrawn. |

The step prints no token.

### 3.4 From a merge to a tag

The release workflow keeps its trigger: a tag `<agent>/v<version>`. Every check that binds a
signer to that tag stays as it is.

A second workflow, rendered by the tooling into each agent repository, runs on a push to
`main`. For each `agents/*/pyproject.toml` whose version has no tag `<agent>/v<version>`, it
creates that tag on the pushed commit, one agent at a time, in name order. It pushes with a
deploy key held in the repository secret `RELEASE_TAG_KEY`, because a tag pushed with the
workflow's own token starts no workflow. When the secret is absent it prints the tags it would
have created and succeeds. Creating the key and letting it past the tag rule are the
repository owner's steps.

The rendered release workflow runs one release at a time (one concurrency group for the
repository, nothing cancelled).

A pull request check fails when files under `agents/<name>/` other than tests and documents
changed and that agent's version did not.

## 4. The Agent in force

`Deployment.document["agent"]` is what the cluster is given as the Agent. It is the Agent
document of one revision, chosen in this order:

1. The candidate's, when the candidate has an approval (carried forward or a person's).
2. The current revision's.
3. The newest revision's, when nothing is current and nothing was ever revoked on this
   deployment.
4. Otherwise it stays as it is.

Every transaction that could change the choice ends by settling it: if the chosen document
differs from the stored one, it is stored and `Deployment.generation` becomes the
transaction's event number. Those transactions: record a release, carry an approval forward,
approve, activate, refuse, supersede, pin, roll back, unpin, and a revocation taking effect.

This replaces `lifecycle.md` section 5 step 6, where a recorded release replaced the Agent at
once. An unapproved candidate now changes nothing that runs.

Known limits, stated in status when they apply:

- While a candidate with an approval is brought up, the identity provider objects follow the
  candidate. If the candidate dropped a tool backend, sessions of the current release lose
  that backend until the candidate is current or refused.
- A current release without a carried declaration reads as drifted while such a candidate is
  brought up. This happens once per agent, on its first release under this contract.

## 5. Phases

A revision's phase is one of `awaiting-runtime`, `pending-approval`, `evaluating`,
`activated`, `superseded`, `refused`. Only the newest revision moves, except by section 8.

Rules that hold in every case:

- Nothing moves in recovery, on the pull request path, or without a fresh observation of the
  AgentRelease whose live generation is at least the revision's generation (`lifecycle.md`
  section 6).
- The pointer moves only at activation, pin, rollback, unpin and revocation.
- `advance` decides once on plain reads and again inside the transaction.
- An evaluation is **required** for a revision when its release's `lineage.evaluation.suite`
  has cases.

The newest revision N, on a usable observation:

| # | N is | And | Then |
| --- | --- | --- | --- |
| D1 | `awaiting-runtime`, no approval | A gate fails | Stays. Reason is the first failure. Event `runtime-verdict`, once per reason. |
| D2 | `awaiting-runtime`, no approval | Gates pass; an approval covers it (`lifecycle.md` section 7) | Approval carried forward. Stays `awaiting-runtime`, reason "approved; waiting for Ready". The Agent in force becomes N's. |
| D3 | `awaiting-runtime`, no approval | Gates pass; no approval covers it | `pending-approval` with the reason. Event `approval-needed`. |
| D4 | `pending-approval` or `awaiting-runtime` | A person approves (`lifecycle.md` section 8) | `awaiting-runtime` with the approval. The Agent in force becomes N's. |
| D5 | Has an approval | Observed approved, not Ready | Stays. Reason "waiting for Ready: <reason>". |
| D6 | Has an approval | Observed approved and Ready; no evaluation required; the current revision was not admitted by an evaluation | `activated`. The pointer moves to N unless pinned. Event `revision-activated`. |
| D7 | Has an approval | Observed approved and Ready; no evaluation required; the current revision was admitted by an evaluation | `refused`, reason "the release carries no evaluation suite and the current release was admitted by one". Event `revision-refused`. |
| D8 | Has an approval | Observed approved and Ready; evaluation required | `evaluating`, reason "waiting for evaluation". Evaluation attempt 1 is created `pending`. Event `evaluation-requested`. |
| D9 | `evaluating` | A result `passed` is recorded | `activated`. The pointer moves to N unless pinned. Event `revision-activated` with the evaluation. |
| D10 | `evaluating` | A result `failed` is recorded | `refused`, reason "evaluation failed: <summary>". The pointer does not move. Event `revision-refused`. |
| D11 | `evaluating` | A result `error` is recorded and fewer than 3 attempts since the last request ended in `error` | Stays. The next attempt is created `pending`. Event `evaluation-requested`. |
| D12 | `evaluating` | A result `error` is recorded and it is the third | `refused`, reason "evaluation could not be completed: <summary>". |
| D13 | `awaiting-runtime`, `pending-approval` or `evaluating` | A newer revision is recorded | `superseded`. Its `pending` and `claimed` evaluations become `cancelled`. |
| D14 | `refused` by D10 or D12, still the newest, release not revoked | An operator asks for another evaluation | `evaluating`. The next attempt is created. Event `evaluation-requested` with the actor. |
| D15 | Any of D6, D9 | The deployment is pinned | N is `activated` and the pointer stays. Status says the pin holds it. |

A revision is **admitted by an evaluation** when a `passed` evaluation exists for it.

After D7, D10, D12 and D13 the Agent in force is settled again (section 4), which returns it
to the current revision's.

An approval of a revision that is `evaluating`, `refused`, `activated` or `superseded` is
refused `stale-approval`, as today.

### 5.1 A new revision of the same release

`revisions.re_resolve(tx, deployment_id, *, now)` records a new revision of the newest
revision's release when its resolutions (`derive.resolutions_of`) differ from the ones stored
for the newest revision. Origin `registry`, event `revision-recorded` with what changed. It is
called for every registry-owned agent after a feed is applied. The new revision goes through
the table above like any other: its approval is carried forward when it is covered, and its
evaluation is required again.

Two revisions then share one AgentRelease object. `projection.md` changes for this:

- One object per release name. Its `revision` annotation and its generation come from the
  newest revision of that release.
- Its approval block comes from the newest revision of that release that has an approval, and
  is `{"approved": false}` when none has, or when the release is revoked.

## 6. Evaluations

### 6.1 The record

`models.Evaluation`. One per attempt. `id` is `<deployment>@<revision>#<attempt>`. `suite` is
the suite the release carried; `suite_digest` is `canonical.digest(suite)`.

| State | Meaning |
| --- | --- |
| `pending` | Waiting for an evaluator |
| `claimed` | An evaluator holds it until `claim_expires_at` |
| `passed`, `failed`, `error` | The verdict. Final. |
| `cancelled` | The revision was superseded or its release revoked. Final. |

`result`, once final:

```json
{
  "verdict": "failed",
  "summary": "1 of 2 cases passed; acceptance needs all",
  "acceptance": {"min_passed": "all"},
  "cases": [
    {"id": "instance-summary", "passed": true, "sessionId": "3f9c0d1e-...", "outcome": "completed", "detail": ""},
    {"id": "blocking", "passed": false, "sessionId": "77ab41c2-...", "outcome": "completed", "detail": "report_contains: \"blocked by\" not found"}
  ]
}
```

`error` means the suite could not be run to a verdict: the session interface refused or could
not be reached, or a case ran past its time. `failed` means every case ran and acceptance was
not met.

### 6.2 Routes

Caller: a service account with the realm role `registry-evaluator` whose token's `azp` is
`registry-evaluator-<cluster>`, checked the way the provisioner's is (`api/auth.py`). The
role name is `EVALUATOR` in `config.py`.

`POST /v1/clusters/{cluster}/evaluations/claim` with `{"evaluator": "<name>", "leaseSeconds": 900}`
(60 to 3600). Answers `{"evaluation": null}` or:

```json
{"evaluation": {
  "deployment": "agent:kubani:tenant-test:db-admin", "revision": 3, "attempt": 1,
  "agent": "db-admin", "tenant": "tenant-test", "namespace": "tenant-test",
  "release": "db-admin-2026-11-20-1a2b3c4d",
  "suite": {"...": "..."}, "suiteDigest": "sha256:...",
  "claimExpiresAt": "2026-11-20T10:15:00Z"}}
```

It gives the oldest evaluation of that cluster that is `pending`, or `claimed` with a lease
that has run out, and whose revision is `evaluating`. It becomes `claimed` by that evaluator.
Event `evaluation-claimed`. Two callers at once get different evaluations or one gets null.
In recovery it answers null.

`POST /v1/deployments/{id}/revisions/{number}/evaluations/{attempt}/result` with

```json
{"evaluator": "<name>", "evaluatorVersion": "0.1.0", "verdict": "passed",
 "summary": "...", "cases": [{"id": "...", "passed": true, "sessionId": "...", "outcome": "completed", "detail": ""}]}
```

| Case | Answer |
| --- | --- |
| The evaluation is `claimed` by this evaluator (the lease may have run out, if nobody else claimed it since) | Recorded. Event `evaluation-recorded`. The revision moves by D9 to D12 in the same transaction. 200 `{"recorded": true, "phase": "<the revision's phase now>"}` |
| It already has this verdict from this evaluator | 200 `{"recorded": false, ...}`; nothing written |
| It is `cancelled` | 200 `{"recorded": false, "phase": "superseded"}` or the phase it has; nothing written |
| It is claimed by another evaluator, or final with another result | 409 `conflict` |
| No such evaluation | 404 `not-found` |
| In recovery | 409 `in-recovery` |

`POST /v1/deployments/{id}/revisions/{number}/evaluations` with `{"reason": "..."}`, role
`registry-operator`: D14. Refused with code `refused` when the revision is not the newest, was
not refused by an evaluation, or its release is revoked.

`GET /v1/deployments/{id}/revisions/{number}` gains `evaluations`, oldest first.

### 6.3 The evaluator

One loop per cluster. Each pass: claim; if there is one, run it; post the result; otherwise
sleep.

- A case is one session started through the session interface with the release named and
  `purpose: "evaluation"`. Its session id is a version 4 UUID made from the first 16 bytes of
  the sha256 of `<evaluation id>/<case id>`, with the version and variant bits set
  (`uuid.UUID(bytes=..., version=4)`). Starting it again returns the session that exists, so
  an evaluator that restarts runs nothing twice.
- Cases run one at a time, in file order.
- A case passes when the session ends `completed` and every check of `expect` holds on its
  report.
- A session still running after `timeout_seconds` is cancelled, and the verdict is `error`.
- A start the session interface refuses (any 4xx answer; the release is not Ready, for
  one), or a session interface that cannot be reached after 5 tries over about a minute,
  makes the verdict `error`. The summary carries the interface's code and message.
- The lease asked for is `timeout_seconds` times the number of cases, plus 120, at most 3600.
- It never handles a grant handle and never starts a session for a release whose identity
  mode is not `agent`; the session interface refuses those, which is `error`.

## 7. Rollback, pin, unpin, disable

A revision is an **eligible target** when all of these hold:

| Check | Refusal (`refused`, with these words) |
| --- | --- |
| Its phase is `activated` | "revision N has not been activated" |
| Its release is not revoked | "release R is revoked (<revocation id>)" |
| The newest active registry approval has the tenant's policy revision, and the target's authority does not widen the authority of that approval's revision | "revision N needs an approval that is no longer in force: <first widening>" |
| A fresh observation of its AgentRelease exists with a current object | the observation's problem, as in approval |
| Its release carries a declaration: the gates pass and Ready is True | "revision N is not Ready: <reason>" |
| Its release carries no declaration: the gates pass with `PolicyDrift` and Ready left out | the first failing gate |

`POST /v1/deployments/{id}/pin` with `{"revision": N}` keeps its shape and now applies this
check. It sets `pinned_revision` and `current_revision` to N. Event `pinned`.

`POST /v1/deployments/{id}/rollback` with `{"revision": N | null, "reason": "..."}`. With
null the target is the newest `activated` revision below the current one whose release is not
revoked; when there is none: `refused`, "there is no earlier release to return to". Then it is
a pin of the target, with event `rolled-back` carrying `from`, `to` and the reason.

`pin` with `{"revision": null}` (unpin): `pinned_revision` is cleared and the pointer moves to
the newest `activated` revision whose release is not revoked, or to none. Event `unpinned`.

Enable and disable are unchanged.

After each of these the Agent in force is settled.

## 8. Revocation

### 8.1 The record in the cluster repository

One reviewed file per revocation, `infrastructure/registry/revocations/<id>.yaml` in the
cluster repository:

```yaml
schemaVersion: 1
id: 2026-11-20-db-admin-1a2b3c4d      # equals the file name without .yaml
kind: agent
item: db-admin
release: db-admin-2026-11-20-1a2b3c4d # the release name
reason: security                      # security | quality
sessions: cancel                      # cancel | finish; default cancel for security, finish for quality
summary: The release leaks query text into its report.
revokedBy: al
revokedAt: 2026-11-20T10:00:00Z
```

A file is never edited or removed after it is merged. The feed carries them as
`revocations`, a list of these objects, in id order. A feed without the key means none.

### 8.2 What the registry does

When a feed is applied, each revocation not yet stored is stored (`models.Revocation`, event
`release-revoked`). A stored revocation that the feed now states differently is an error in
the feed and the whole feed is refused, as for any invalid feed. A stored revocation missing
from the feed stays stored.

A release is **revoked** when a revocation exists with its kind, item and release name.
`rules.revocation.revoked(reads, release) -> Revocation | None` answers it. Only `agent`
revocations have an effect in milestone 2; others are stored.

Effects, in the transaction that stores the revocation, for every registry-owned deployment
with a revision of that release:

| What | Effect |
| --- | --- |
| Each revision of that release | Its generation is raised, so the object is written again, now with `{"approved": false}` and the annotation `registry.agentplatform.io/revoked: <id>` |
| Such a revision that is `awaiting-runtime`, `pending-approval` or `evaluating` | `refused`, reason "release revoked: <id>". Its open evaluations become `cancelled`. |
| Such a revision that is current | The pointer is cleared and so is the pin if it named that revision. Event `current-cleared`. Status says there is no current release and names `rollback` as the next action. The registry does not choose a replacement by itself. |
| Such a revision that is `activated` and not current | Stays `activated`. It is never an eligible target (section 7). |

A report of a revoked release is refused (section 3.2). Recording nothing is right: the
revocation is the record.

On the pull request path a revocation is stored and shown in status, and changes nothing on
the cluster.

Because the feed is applied when the registry starts and before anything is given to a
cluster, a database restored from before a revocation ends in the same state as one that saw
it arrive.

### 8.3 Sessions

`GET /v1/clusters/{cluster}/desired` gains

```json
"revocations": [{"id": "...", "tenant": "tenant-test", "namespace": "tenant-test",
                 "agent": "db-admin", "release": "db-admin-2026-11-20-1a2b3c4d", "sessions": "cancel"}]
```

listing `agent` revocations with `reason: security` and `sessions: cancel` that were stored
less than `REGISTRY_REVOCATION_CANCEL_SECONDS` ago (default 86400) and whose release has a
revision on a registry-owned deployment of that cluster.

The provisioner, on each pass, after it has written that release's AgentRelease and seen the
write succeed, asks the tenant's session interface to cancel the release's sessions
(`sessions.md`, cancel by release). It never cancels before the write. A failure is logged and
tried again on the next pass. With `sessions: finish`, or `reason: quality`, the revocation is
not in the list and running sessions end by themselves.

## 9. What the cluster is given

Additions to `projection.md`:

- The Agent is the Agent in force (section 4).
- The Agent's annotation `registry.agentplatform.io/current-release` is unchanged: the current
  revision's release name, absent when nothing is current. The session interface and the
  controller's rate limit read it.
- AgentRelease objects follow section 5.1 and section 8.2.
- `desired` gains `revocations` (section 8.3).

**Acknowledged** is a status fact, not a phase: the last observation of the Agent is fresh and
its live generation is at least `Deployment.generation`. Until then status says the cluster
has not taken the last change.

## 10. Status

The agent's entry in `GET /v1/status` gains:

| Field | Content |
| --- | --- |
| `current` | `{release, revision, since, admittedBy}`; `admittedBy` is the evaluation id or null |
| `candidate` | `{release, revision, phase, reason, evaluation}` when there is one; `evaluation` is `{attempt, state}` |
| `lastRefused` | `{release, revision, reason, at, evaluation}` for the newest `refused` revision when it is newer than the current one. `evaluation` has the verdict and summary. |
| `acknowledged` | Section 9 |
| `pinned` | The pinned revision or null |
| `revoked` | Revocations of this item, newest first: `{id, release, reason, at}` |

Blockers and next actions follow `lifecycle.md` section 12, with these added: a refused
candidate (fix and release again, or ask for another evaluation); no current release after a
revocation (`rollback`); an evaluation `pending` for more than 10 minutes (the evaluator is
not running).

## 11. Events

New types: `evaluation-requested`, `evaluation-claimed`, `evaluation-recorded`,
`revision-refused`, `revision-recorded`, `rolled-back`, `release-revoked`, `current-cleared`,
`agent-settled` (only when a transaction's sole change is the Agent in force).

## 12. What must be true on the cluster

Not part of this repository; listed so that nothing is assumed.

| Fact | State on 2026-11-18 | Owner |
| --- | --- | --- |
| The agents gateway accepts the alias as the requested model name on its backend | **False.** Each backend's model-pin policy accepts only the served name. It must accept the served name or the alias. | Cluster repository, package 2.10 |
| The gateway sends the pinned model upstream whatever name the caller used | Configured (`openai.model` on the backend) but never observed. Observed in the acceptance run. If it does not hold, the engine serves the alias as a second name, which restarts the engine. | Acceptance run |
| The CRD with `spec.declaration` is installed before the controller that reads it, and both before the first one-file release | | Cluster repository, package 2.10 |
| The registry can reach GitHub's signing keys, and the release runner can reach the registry | | Cluster repository, package 2.10 |
| Roles `registry-evaluator` and its service client exist in the realm | A person's step | Operator |

## 13. Which package changes what

| Package | Files it owns | Sections |
| --- | --- | --- |
| Store | `store/memory.py`, `store/pg.py`, `tests/store_suite.py`, `tests/test_migrations.py`, `tests/world.py` (helpers for the new records only) | The protocol in `store/__init__.py` and `migrations/0002_delivery.sql`, as written |
| 2.2 Controller | `platform/controller/`, `deploy/platform/crds/` | 2, and the rate limit from `current-release` |
| 2.1 Session interface | `platform/session-api/`, and the `memo` argument in `platform/runner/runner/submit.py` | `platform/session-api/contract/sessions.md` |
| 2.3 Report | `agent_registry/api/intake.py`, `agent_registry/api/github.py`, `agent_registry/rules/report.py` (the suite and declaration checks, which 2.4 then also calls), `agent_registry/config.py`, the wiring in `api/app.py`, `.github/actions/agent-release/` | 1.3 (the registry's check), 3.1 to 3.3 |
| 2.4 Activation | `rules/activation.py`, `rules/revisions.py`, `rules/lifecycle.py`, `rules/approval.py`, `rules/projection.py`, `api/write.py`, `api/read.py`, `api/evaluations.py`, `api/auth.py`, `api/deps.py`, `api/app.py`, `config.py`, `queries.py`, `api/schemas.py`, `tests/world.py`, `contract/lifecycle.md`, `contract/projection.md`, `contract/openapi.json` | 4, 5, 6.1, 6.2, 7, 8.2 (what the AgentRelease object says of a revoked release), 9, 10, 11 |
| 2.5 Evaluator | `platform/evaluator/` | 6.3 |
| 2.6 Provisioner | `platform/provisioner/` | 2 (the object is written whole), 8.3, 9 |
| 2.7 Tooling | `platform/agent-tools/` | 1, 2 (the tooling row), 3.4 |
| 2.8 Revocation | `rules/revocation.py`, `importer/feed.py`, `api/cluster.py`, `api/intake.py` (the refusal of a revoked release's report) | 3.2 (that refusal), 8 except the object's content |

2.4 and 2.8 run together. 2.8 calls nothing of 2.4 except
`revisions.settle_agent(tx, deployment_id, seq)` and
`revisions.cancel_open_evaluations(tx, deployment_id, number, *, now)`; 2.4 calls nothing of
2.8 except `revocation.revoked(reads, release) -> Revocation | None`. These three functions
are written, with tests, before either package starts, and neither package changes their
signatures. `schemas.DesiredRevocation` and `DesiredOut.revocations` are written before too;
2.8 fills the list and does not change `api/schemas.py`. The call of `revisions.re_resolve`
after a feed is applied (section 5.1) is added when the two packages are joined.
