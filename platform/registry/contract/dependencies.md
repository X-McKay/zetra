# Dependency changes: tool contracts, model releases and alias moves

Milestone 3. `delivery.md` gets an agent release from a merge to a running agent. This
document covers the other direction: a tool server or a model changes, and no agent release
does. The goal is that such a change cannot break an agent unnoticed.

Where this document and `delivery.md`, `import.md` or `projection.md` disagree about new
behaviour, this one is right. Everything they say that is not changed here still holds.

Cases as data: `impact-cases.json` (section 3) and `alias-move-cases.json` (section 6).

## 1. What is in, and what waits

In: a recorded contract per tool server; a check that says which agents a proposed contract
breaks; a manifest per model release; an assessment of an alias move; a hold on the agents
behind an alias while its engine is swapped; evaluation of those agents on the new model; and
a check that a pull request in the cluster repository can ask for both kinds of change.

Waits, on purpose:

- Comparing the contract seen through the gateway after a move with the recorded one.
- A session worker that refuses to start when the model behind the alias is not the one the
  agent's revision names. The hold and the check of section 8 are the protection for now.
- Moving an alias automatically, and noticing by itself that the engine is serving again. A
  person says so (transition A9).
- Deciding from the kind of change which evaluations may be reused. As in milestone 2, a
  changed resolution makes a new revision and the whole suite runs.
- Scanning tool server images, and any signature or attestation of a contract or manifest.
- Holding agents on the pull request path. They are listed and not held.

## 2. Tool contract record

### 2.1 The file in the cluster repository

One generated, reviewed file per tool server, `infrastructure/mcp/contracts/<server>.json`.
It is what the server answered to `tools/list`, plus the effect the operator assigns:

```json
{
  "schemaVersion": 1,
  "server": "postgres-admin-mcp",
  "backend": "postgres-admin",
  "image": "docker.io/crystaldba/postgres-mcp@sha256:dbbd3468...",
  "configDigest": "sha256:...",
  "tools": [
    {
      "name": "execute_sql",
      "description": "Execute any SQL query",
      "effect": "remote_read",
      "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
      "outputSchema": null
    }
  ]
}
```

- `server` is the tool server's name in the feed. `backend` is its backend name, or `null`
  for a server that is not registered.
- `image` is the image the answer came from, with its digest.
- `configDigest` is `sha256:` and the hex SHA-256 of the canonical JSON (sorted keys, no
  spaces, UTF-8) of `{"command": [...], "args": [...], "env": [...]}` of the server's
  container as the manifest states it. `env` is the list of `{name, value}` or
  `{name, valueFrom}` entries sorted by name; a secret is named by its reference, never by
  its value. What a server offers depends on its flags as well as its image.
- `tools` is sorted by name. `description` is the text exactly as listed. `inputSchema` is
  the listed JSON Schema; `outputSchema` is the listed one or `null`.
- `effect` is `remote_read` or `remote_write`. Upstream does not define it. A tool that the
  backend's definition in the cluster repository authorises (`authorization.tools`) is
  `remote_read`: those are the tools reviewed for agents, and `remote_read` is the only
  effect an agent can be granted (`agent-spec.schema.json`). Every other tool, and every
  tool of a server that is not registered, is `remote_write`.
- The file has no time and no host in it. Capturing an unchanged server twice gives the same
  bytes.

### 2.2 In the feed

Each `toolServers` entry gains two keys:

| Key | Value |
| --- | --- |
| `configDigest` | The digest of section 2.1, computed from the manifest as committed |
| `contract` | The file's content as an object, or `null` when the server has no file |

A feed is invalid when a contract's `server` or `backend` differs from the entry's, when its
`configDigest` differs from the entry's, or when the entry's image is pinned by digest and
the contract's image names another digest. So an image or flag change without a new capture
does not render. A server with no contract file is valid.

A feed without these keys (an older cluster repository) is read as `configDigest: ""` and
`contract: null`.

### 2.3 In the registry

The tool server's `Deployment.document` gains `configDigest` and `contract`, where
`contract` is `null` or

```json
{"digest": "sha256:...", "image": "...", "configDigest": "...",
 "tools": {"execute_sql": {"description": "...", "effect": "remote_read",
                           "inputSchema": {...}, "outputSchema": null,
                           "digest": "sha256:..."}}}
```

A tool's `digest` is `canonical.digest` of its four other fields. The contract's `digest` is
`canonical.digest` of the map of tool name to tool digest. The contract belongs to the
deployment and not to the release, because flags change what an image offers.

`derive.resolutions_of`: the `detail` of a `tool` resolution keeps `image` and gains
`contract`, the `canonical.digest` of the map of each required tool name to that tool's
digest (`null` for a required tool the contract does not list). The key is absent when the
server has no contract. So `revisions.re_resolve` (`delivery.md` section 5.1) records a new
revision of an agent when the contract of a tool it requires changes, and not when another
tool of that server changes.

One consequence, stated so that nobody is surprised: the first feed that carries a contract
for a server gives every registry-owned agent that uses it one new revision, and that
revision is evaluated.

`GET /v1/status` and `GET /v1/items/tool-server/<name>` show, per tool server, `contract`:
`recorded` with the digest and the number of tools, or `none`.

## 3. Impact of a proposed contract

### 3.1 Who is asked about

The **users** of a tool server on a cluster are the agent deployments of that cluster, of
either authority, enabled or not, that have a requirement of type `tool` whose `target` is
the server's backend, on the release of their current revision or on the release of their
newest revision when that revision is not `refused` or `superseded`. A user's **required
tools** are the `names` of those requirements, joined.

A server with `backend: null` has no users.

### 3.2 Findings

`rules.impact.tool_contract(reads, cluster, server, proposed) -> Impact`. `proposed` is a
contract file's content. `recorded` is the contract stored for the server, or none.

For each user and each of its required tools, the first row that applies:

| # | Condition | Finding | Blocks |
| --- | --- | --- | --- |
| I1 | The tool is not in `proposed` | `tool-removed` | yes |
| I2 | Its `effect` in `proposed` is not `remote_read` | `effect-widened` | yes |
| I3 | `recorded` lists the tool and the input schemas are incompatible (below) | `input-incompatible` | yes |
| I4 | `recorded` lists the tool and the input schema differs in any other way | `input-changed` | no |
| I5 | `recorded` lists the tool and the description differs | `description-changed` | no |
| I6 | `recorded` lists the tool and the output schema differs | `output-changed` | no |
| I7 | Otherwise | none | |

A tool in `proposed` that no user requires produces nothing, whether it is new or not.

**Incompatible** input schemas, comparing only the top level of each object schema
(`properties` and `required`):

- a property name is in the proposed `required` and not in the recorded `required`; or
- a property the recorded schema has is absent from the proposed schema; or
- a property both have has a different `type`.

A missing `properties` is `{}` and a missing `required` is `[]`. Nested schemas are compared
only for equality (which decides I4), not for compatibility.

### 3.3 The answer

`POST /v1/checks/tool-contract`, role `registry-reader` (an operator may also call it):

```json
{"cluster": "kubani", "server": "postgres-admin-mcp", "contract": { ...the file... }}
```

```json
{
  "server": "postgres-admin-mcp",
  "backend": "postgres-admin",
  "blocked": true,
  "findings": [
    {"deployment": "agent:kubani:tenant-test:db-admin", "agent": "db-admin",
     "tenant": "tenant-test", "authority": "pull-request", "tool": "execute_sql",
     "finding": "tool-removed", "blocks": true,
     "action": "keep execute_sql on postgres-admin, or release db-admin without it first"}
  ],
  "reevaluated": [],
  "users": 1
}
```

- `findings` is sorted by deployment and then tool. `blocked` is true when any finding
  blocks.
- `reevaluated` lists the registry-owned users that have at least one finding and none that
  blocks: they get a new revision when the contract is merged, and it is evaluated.
- `action` is one sentence a person can act on: for `tool-removed` and
  `input-incompatible`, keep the tool as it was or release the agent first; for
  `effect-widened`, the tool is no longer read-only and the agent must stop using it; for the
  others, "evaluated again after the change" for a registry-owned agent and "check the agent
  by hand" for one on the pull request path.
- `not-found` when the cluster or the server is unknown. `invalid-arguments` when the
  contract is not a contract for that server.
- Nothing is written. The check reads the store, so with the database down it answers
  `unavailable` and the caller treats that as a failed check.

## 4. Model release manifest

### 4.1 The file in the cluster repository

One reviewed file per model release, `infrastructure/registry/models/<engine>/<name>.yaml`:

```yaml
schemaVersion: 1
engine: fast
name: Qwen-Qwen3.5-0.8B            # the file name without .yaml; a release_slug
servedName: Qwen/Qwen3.5-0.8B
weights:
  source: hf://Qwen/Qwen3.5-0.8B
  revision: null                   # null when it was not recorded; say so, do not guess
  files: null                      # or a map of file name to sha256
format: safetensors
quantisation: null
engineImage: vllm/vllm-openai:v0.30.0-aarch64
serving:
  chatTemplate: null               # null: the one shipped with the weights
  toolCallParser: null
  contextLength: null
offers:
  contextLength: null
  toolCalling: true
  structuredOutput: null
  embeddingSize: null
evidence:
  benchmark: docs/infrastructure/inference/benchmarks/fast/20261001T0409Z_candidate-v0.30.0.json
  changeRecord: docs/infrastructure/inference/releases/2026-09-26-vllm-v0.30.0.md
```

`schemaVersion`, `engine`, `name`, `servedName`, `weights.source`, `engineImage` and `offers`
are required. Every value in `offers` and `serving` may be `null`, which means not recorded.
A manifest is never edited after it is merged; a change is a new release with a new name.

The release each engine serves today keeps the name it was adopted under,
`release_slug(servedName)`, so that adding its manifest changes no agent.

### 4.2 In the feed

- New top-level key `modelReleases`: the manifests as objects, sorted by engine and name.
- Each `modelEngines` entry gains `release`: the name of the manifest the engine serves. The
  manifest must be for that engine, its `servedName` must equal the entry's and its
  `engineImage` must equal the image of the engine's container in the manifest. So a new
  engine image or a new served name does not render without a new model release.
- Each `aliases` entry may carry `move`: the id of the alias move (section 6) the change
  relies on. It is a record for the reader of the file; the registry matches a move by alias
  and candidate, not by this field.

A feed without `modelReleases` is read as none, and an engine without `release` serves
`release_slug(servedName)`, as before.

### 4.3 In the registry

Each manifest is a `Release` of kind `model`, item the engine, `name` the manifest's name,
`origin = "adopted"`, `document` the manifest, `digest` `canonical.digest` of the manifest
without `evidence`. A release adopted earlier whose document is only `{"servedName": ...}`
is completed once by a manifest of the same name and served name. After that a stored
manifest that the feed states differently makes the feed invalid.

A model engine's `Deployment.document` gains `release`, the name of the release it serves.

`derive.resolutions_of`: a `model` resolution's `resolved_release` is the id of the release
the engine serves, and its `detail` gains `release`, that release's name. So an engine that
moves to a new release gives its dependants a new revision even when the served name is
unchanged. As in section 2.3, the first feed with `release` gives every registry-owned agent
one new revision.

When the engine's entry in the feed has no `release`, the resolution is exactly as in
milestone 2: no `resolved_release` and no `release` in `detail`. So a registry that reads an
older feed records no new revision for anybody, and the new revision comes with the reviewed
change to the feed, not with the registry's own roll-out.

A feed is invalid when an engine's `release` names a manifest that is neither in
`modelReleases` nor stored, a manifest of another engine, or one whose `servedName` differs
from the entry's. The registry does not see engine images; the comparison of `engineImage`
with the engine's container is made where the feed is rendered.

**The pair behind an alias** is `{engine, release}`: the engine the alias points at and the
release that engine serves. An alias **moves** when its pair changes, whether the alias
points at another engine or the engine takes another release.

## 5. Assessment

`POST /v1/assessments`, a person with `registry-operator`:

```json
{"cluster": "kubani", "alias": "fast", "candidate": {"engine": "fast", "release": "Qwen3.5-0.8B-r2"}}
```

The candidate's release must be stored (its manifest is merged first; that is the *prepare*
step) and must differ from the pair behind the alias now. The answer is the stored
assessment:

```json
{
  "id": "asm-...",
  "cluster": "kubani", "alias": "fast",
  "from": {"engine": "fast", "release": "Qwen-Qwen3.5-0.8B"},
  "candidate": {"engine": "fast", "release": "Qwen3.5-0.8B-r2"},
  "dependants": [
    {"deployment": "agent:kubani:tenant-test:alias-rehearsal-pass", "agent": "alias-rehearsal-pass",
     "tenant": "tenant-test", "authority": "registry", "enabled": true,
     "release": "alias-rehearsal-pass-2026-11-19-0a1b2c3d",
     "held": true, "evaluation": "required",
     "needs": [{"need": "toolCalling", "met": true}]}
  ],
  "blocked": false,
  "digest": "sha256:...",
  "createdBy": "al@example.com", "createdAt": "..."
}
```

- The **dependants** of an alias are the agent deployments of the cluster, of either
  authority, that have a `model` resolution whose `detail.alias` is the alias, on their
  current revision or on their newest revision when that is not `refused` or `superseded`.
  Sorted by deployment id.
- `release` is the name of the release of the current revision, or `null`.
- `held` is true for a dependant that is registry-owned and enabled. A dependant on the pull
  request path is listed with `held: false`: its sessions will use the new model with no
  hold and no evaluation, and the person reading the assessment decides whether that is
  acceptable.
- `evaluation` is `required` when the release's suite has cases (`activation.required`),
  else `none`.
- `needs`: one need exists in this milestone. A dependant whose release has any `tool`
  requirement needs `toolCalling`. `met` is the candidate manifest's `offers.toolCalling`:
  `true`, `false`, or `null` when the manifest does not say. `blocked` is true when any need
  of an enabled dependant has `met: false`.
- `digest` is `canonical.digest` of `{cluster, alias, from, candidate, dependants}` with each
  dependant reduced to `{deployment, authority, enabled, release}`.

An assessment is **stale** when computing it again now gives another digest: the pair behind
the alias changed, a dependant appeared or left, or a dependant's authority, enabled flag or
current release changed.

`GET /v1/assessments/{id}`, reader: the assessment, with `stale: true|false` computed at the
time of the call.

## 6. Alias move

### 6.1 Records

`AliasMove`: `id`, `assessment_id`, `cluster`, `alias`, `candidate`, `state`, `sessions`
(`finish` or `cancel`), `evaluation_seconds`, `started_by`, `started_at`, `held_at`,
`moved_at`, `evaluating_at`, `deadline`, `finished_at`, `reason`.

`Hold`: `move_id`, `deployment_id`, `state`, `reason`, `running` (the last reported number of
running task sessions, or `null`), `acknowledged_at`, `ended_at`. A deployment has at most
one hold that is not `resumed` or `released`.

Move states: `holding`, `held`, `moving`, `evaluating`, `completed`, `cancelled`. A move is
**open** until it is `completed` or `cancelled`. An alias has at most one open move.

Hold states: `holding`, `held`, `suspended`, `resumed`, `released`. A hold is **in force**
while it is `holding`, `held` or `suspended`.

### 6.2 Transitions

| # | When | What happens |
| --- | --- | --- |
| A1 | An operator starts a move for an assessment that is not stale and not blocked, and the alias has no open move | The move is stored as `holding`, with one hold in state `holding` per dependant that has `held: true`. Each held deployment's generation is raised so that its Agent is written again (section 7). Event `alias-move-started`. With no dependant to hold, the move is stored as `held` at once. |
| A2 | The same, and the assessment is stale | Refused, code `stale-assessment`. Nothing is written. Staleness is decided first, then A3. |
| A3 | The same, and the assessment is blocked, or the alias has an open move | Refused, code `refused` for a blocked assessment and `conflict` for an open move. |
| A4 | An observation report acknowledges a hold (section 7.3) with no task session running | The hold becomes `held`. When every hold of the move is `held`, the move becomes `held` and `held_at` is set. Event `alias-move-held`. |
| A5 | An operator cancels a move that is `holding` or `held` | The move becomes `cancelled`; its holds become `released`; the generations are raised. Event `alias-move-cancelled`. A move in any other state cannot be cancelled: `refused`. |
| A6 | A feed is applied in which the pair behind the alias equals the move's candidate, and the move is `held` | The move becomes `moving` and `moved_at` is set. Event `alias-moved`. `re_resolve` then records the dependants' new revisions as in milestone 2. |
| A7 | The same, and the move is still `holding` | As A6, with a second event `alias-moved-before-hold-confirmed`. The check of section 8 exists to prevent this; if it was bypassed, the registry still follows the cluster. |
| A8 | A feed is applied in which the pair behind the alias changes to something that is not the candidate of an open move | Event `alias-changed-without-move` naming the alias and both pairs. An open move of that alias that is `holding` or `held` becomes `cancelled` (as A5) with the reason "the alias moved to another candidate". Milestone 2's behaviour applies: new revisions, evaluated, nobody held. |
| A9 | An operator says the engine is serving, the move is `moving`, and the newest observation of the candidate's engine is fresh, shows `readyReplicas` of at least 1 and shows the candidate's served name | The move becomes `evaluating`; `evaluating_at` is now and `deadline` is now plus `evaluation_seconds`. Event `alias-move-evaluating`. If the observation does not show that, `refused` with what is missing. |
| A10 | While the move is `evaluating` or `completed`, a revision of a deployment whose hold is in force is activated (D6 or D9) | The hold becomes `resumed`; the generation is raised. Event `hold-resumed`. |
| A11 | While the move is `evaluating`, the newest revision of a deployment whose hold is `held` becomes `refused` | The hold becomes `suspended` with the revision's reason. Event `hold-suspended`. |
| A12 | The move is `evaluating` and the deadline has passed | Every hold still `held` becomes `suspended`, reason "evaluation did not finish in the time allowed", with one event `hold-suspended` each. An evaluation still running is left to finish; if it passes later, A10 resumes the agent. |
| A13 | The move is `evaluating` and no hold is `held` | The move becomes `completed`; `finished_at` is set. Event `alias-move-completed`. |

**Waiting.** While a deployment has a hold in force and the hold's move is `holding`, `held`
or `moving`, transitions D6 and D8 of `delivery.md` do not happen for it: the revision stays
`awaiting-runtime` with the reason `held by alias move <id>`. An evaluation must not run
against an engine that is being swapped. When the move becomes `evaluating` or `cancelled`
the next pass decides as usual.

**Suspended** means the hold stays in force after the move is complete: the agent takes no
new task sessions, because its only evaluated revision was evaluated on the model that is
gone. It ends by A10: a later revision passes (a new release of the agent, or the same one
evaluated again with `POST .../evaluations` after D14), or the alias moves back and the
agent passes there. `enabled` is not used for this, so that the evaluator can still start
sessions on the agent.

**Enabling.** `POST /v1/deployments/{id}/enabled` with `true` is refused, code `refused`, for
a dependant of an alias that has an open move: it would start taking sessions with no hold.

**Where A12 and A13 run.** `rules.alias_move.advance(store, cluster, *, now)` applies A12 and
A13. The observation route calls it after `lifecycle.advance_cluster`, so it runs on every
provisioner pass.

**The database.** Every transition is a transaction on the store. With the database down a
move cannot start and the check of section 8 cannot pass.

### 6.3 Routes

| Route | Role | Does |
| --- | --- | --- |
| `POST /v1/alias-moves` `{"assessment": id, "sessions": "finish"\|"cancel", "evaluationSeconds": n}` | operator, a person | A1 to A3. `sessions` defaults to `finish`. `evaluationSeconds` defaults to `REGISTRY_ALIAS_MOVE_EVALUATION_SECONDS` (default 1800) and must be between 60 and 86400. |
| `GET /v1/alias-moves?cluster=&alias=&open=true` | reader | Moves, newest first |
| `GET /v1/alias-moves/{id}` | reader | The move, its assessment's id and candidate, its holds with agent names, and `next`: one sentence saying what it waits for |
| `POST /v1/alias-moves/{id}/evaluate` | operator, a person | A9 |
| `POST /v1/alias-moves/{id}/cancel` `{"reason": "..."}` | operator, a person | A5 |

A move's `next` by state: `holding`: "waiting for N agent(s) to stop: <names>"; `held`:
"merge the change that moves the alias"; `moving`: "restart or wait for the engine, then say
it is serving"; `evaluating`: "evaluating N agent(s) until <deadline>"; `completed`:
"done", followed by the suspended agents if any.

## 7. The hold in the cluster

### 7.1 What the registry gives

The Agent object of a deployment with a hold in force carries the annotation
`registry.agentplatform.io/held: <move id>`. Without one the annotation is absent.

`GET /v1/clusters/{cluster}/desired` gains

```json
"holds": [{"move": "...", "deployment": "agent:kubani:tenant-test:db-admin",
           "tenant": "tenant-test", "namespace": "tenant-test", "agent": "db-admin",
           "sessions": "finish"}]
```

listing the holds in state `holding`.

### 7.2 The session interface

- Start, after the check that the agent is enabled: when the Agent carries the `held`
  annotation and the caller is an operator, the start is refused, code `refused` (409), with
  the message `the agent is held by alias move <id>`. The evaluator is not refused, so the
  agent can be evaluated on the new model.
- Cancel by release (`sessions.md` section 4.5) accepts an optional `"purpose"`; with it only
  sessions of that purpose are cancelled. `release` becomes optional: without it, every
  running session of the agent that matches.

### 7.3 The provisioner

On each pass, for each entry of `holds`, after it has written that Agent with the annotation
and seen the write succeed (or found the object already carrying it):

1. With `sessions: cancel`, it asks the session interface to cancel the agent's running
   sessions of purpose `task`, reason `alias move <id>`.
2. It lists the agent's running sessions and counts those of purpose `task`.

Its observation report gains

```json
"holds": [{"move": "...", "deployment": "...", "running": 0}]
```

with one entry per hold it could count. A hold whose Agent it could not write, or whose
sessions it could not list, is left out and tried again on the next pass. With no session
interface configured nothing is reported, and the hold is never confirmed.

The registry stores `running` on the hold and applies A4 when it is 0.

### 7.4 The evaluator

No change of behaviour: it claims the evaluation requests that D8 creates once the move is
`evaluating`. Its test for this milestone shows a held agent being evaluated.

## 8. The check a pull request asks for

`POST /v1/checks/alias`, role `registry-reader`:

```json
{"cluster": "kubani", "alias": "fast", "candidate": {"engine": "fast", "release": "Qwen3.5-0.8B-r2"}}
```

```json
{"allowed": true, "move": "...", "assessment": "asm-...", "reason": ""}
```

`allowed` is true only when the alias has a move in state `held` whose candidate is this
pair and whose assessment is not stale. Otherwise `reason` says which of these failed: no
move for this candidate; the move is still `holding` (with the agents not yet stopped); the
assessment is stale (with what changed). Nothing is written.

The cluster repository's check compares the feed rendered from the pull request with the
feed of its base. For each tool server whose `contract` differs it calls
`/v1/checks/tool-contract` and fails when the answer is blocked. For each alias whose pair
differs it calls `/v1/checks/alias` and fails when the answer is not allowed. It fails when
the registry cannot be reached. It runs the base branch's code and reads the pull request's
files only as data.

## 9. Status

`GET /v1/status` gains `aliasMoves`: the open moves, each as in `GET /v1/alias-moves/{id}`.
Each agent in status gains `held`: `null`, or `{"move": id, "state": "holding"|"held"|"suspended",
"reason": "..."}`. A model engine shows `release`.

## 10. Errors

`stale-assessment` is a new code: status 409, not retryable. The cluster repository's
commands pass it through as their outcome code. Everything else uses the existing codes.

## 11. The agent repository

`zetra-agents check --contracts DIR` reads the contract files in `DIR` (copies of
section 2.1 files, named `<server>.json`). For each tool grant of each agent whose backend
some contract names: every granted tool must be in the contract, and its effect there must
be the grant's effect. A grant whose backend no file names is not an error and is reported
as not checked. Without `--contracts` nothing changes.

## 12. The rehearsal

Two small agents in the agents repository, registered in `tenant-test` and taken over by the
registry, both with `model.name: fast`, no tools, and a task that asks the model for one
fixed word:

- `alias-rehearsal-pass`: its suite expects the word.
- `alias-rehearsal-fail`: its report also states the model name the gateway answered with,
  and its suite expects the served name of the release in use before the move. After the
  move it fails by construction.

The rehearsed move gives `fast` a second release that differs only in its served name, and a
second move puts the first one back.
