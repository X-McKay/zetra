# Lifecycle: releases, revisions, approval and the pointer

Milestone 1. This is the contract for the write routes of a registry-owned agent
(`agent_registry/api/write.py`) and the rule modules under `agent_registry/rules/`.
`tests/test_write_cases.py` is the executable form of the cases in section 10.

Two pure rule modules are part of the contract and already written:

- `rules/gates.py`: `gate_failures(release_document, observed, pin)`. Cases:
  `contract/gate-cases.json`.
- `rules/authority.py`: `widens(base, candidate)`. Cases: `contract/authority-cases.json`.

Shared helpers are in `agent_registry/derive.py`: `freshness`, `condition`,
`live_generation`, `pin_for`, `agent_release_record`, `requirements_of`, `resolutions_of`.

## 1. Words

- **Deployment**: an agent on a cluster for a tenant. Id `agent:<cluster>:<tenant>:<name>`.
- **Revision**: one release of that deployment, numbered from 1. The newest is the one with
  the highest number.
- **Current revision**: the revision the deployment's pointer names. It is what a session
  starts on.
- **Authority** of a revision: `canonical.authority_of(<the Agent published with it>)`.
- **Policy revision**: `Tenant.policy_revision`. It changes when the tenant's policy changes.
- **Observation**: what the cluster's provisioner last reported about an object. It is
  *fresh* when `derive.freshness(...) == "fresh"` with the limit
  `Settings.observation_max_age_seconds`.
- **Live generation**: `derive.live_generation(observation.document)`: the registry generation
  the object on the cluster was last written at.

## 2. Phases of a revision

| Phase | Meaning |
| --- | --- |
| `awaiting-runtime` | Projected. Waiting for the controller's verdict, or (once approved) for Ready. `reason` says what it waits for. |
| `pending-approval` | The controller's gates pass and no approval covers the change. A person must approve. |
| `activated` | It has been the current revision. |
| `superseded` | A later revision was recorded before this one was activated. |

`evaluating` and `refused` are not used in milestone 1.

## 3. Generations

The provisioner applies an object only when the registry's generation for it is higher than
the one on the cluster. So every change to something an object is built from must raise that
object's generation, to the sequence number of the event recorded in the same transaction.

- `Deployment.generation` (the projected Agent): raised when the Agent document in force,
  `enabled`, or `current_revision` changes, and when authority is taken.
- `Revision.generation` (the projected AgentRelease): set when the revision is created, when
  an approval is attached to it, and when authority is taken. A change of phase alone does
  not change the object and does not raise it.

## 4. Checks every write route makes first

In this order, before anything else. A refusal writes nothing.

1. `in-recovery`: the registry is in recovery (the `writable` dependency).
2. `not-found`: the deployment does not exist, or its kind is not `agent`.
3. `wrong-path`: the deployment's authority is `pull-request`. The message names the path
   that owns it and the command that takes authority
   (`just agent authority take <agent>`).

The approval preview is a read, but it is served by the same router and makes the same
checks: there is nothing to approve at the registry for an agent it does not own.

**One transaction per request.** A write route opens its transaction first and makes every
check inside it, reading through the transaction (`with store.transaction(actor) as tx`, then
`tx.deployment(...)` and so on). That includes the mode: the `writable` dependency answers a
request early, and the route checks `tx.meta().mode` again. A refusal is raised inside the
transaction, which then writes nothing; a request that finds nothing to do leaves it without
a write and without an event. Writers are serialised by the store, so of two requests at
once the second is decided on what the first wrote. A check made on a plain read before the
transaction opens can be out of date by the time the write happens, and is not a check.

The logic of each route is a function in `agent_registry/rules/` that takes the transaction;
the handler opens the transaction, calls it and shapes the answer. A rule refuses by raising
`rules.Refusal(code, message)`, which leaves the service as the error body with that code.

## 5. Record a release

`POST /v1/deployments/{id}/releases` with `{agent, agentRelease, lineage}`: the two documents
the release workflow published.

Refused with `invalid-arguments` when:

- `agent` is not an `Agent` or `agentRelease` not an `AgentRelease` of
  `agentplatform.io/v1alpha1`;
- `agent.metadata.name`, `agentRelease.spec.agent` and the deployment's item are not all the
  same name;
- either document's namespace is not the deployment's namespace;
- the release name does not match `models.RELEASE_NAME`;
- `agentRelease.spec.approval.approved` is `true`. Approval never arrives in a document.
  (`approval: {approved: false}` or no approval block is accepted; the block is not stored.)

Then:

- If a revision of this deployment already has this release and the stored release document
  equals `canonical.release_document(agentRelease)`: answer with that release and revision
  and `created: false`. Nothing is written.
- If a release with this id exists with a different document: `conflict`.
- Otherwise, in one transaction:
  1. event `release-recorded` (`detail`: release name, revision number, authority digest);
     its sequence number is `seq`;
  2. the `Release` (`derive.agent_release_record`, with the lineage) and its requirements
     (`derive.requirements_of`);
  3. the `Revision` with the next number: origin `registry`,
     `agent_document = canonical.agent_document(agent)`, its authority and authority digest,
     the tenant's `policy_revision`, phase `awaiting-runtime`, reason
     `waiting for the controller's verdict`, `generation = seq`;
  4. its resolutions (`derive.resolutions_of`);
  5. every earlier revision in `awaiting-runtime` or `pending-approval` becomes `superseded`;
  6. the deployment's `document.agent` becomes the new Agent document and its
     `generation = seq`.

Answer: the release, the revision, `created: true`.

## 6. Advance

`rules.lifecycle.advance(store, deployment_id, *, now, max_age_seconds, actor="registry") -> bool`
moves the newest revision forward from what the cluster reports. It returns whether anything
changed. `rules.lifecycle.advance_cluster(store, cluster, *, now, max_age_seconds) -> list[str]`
calls it for every registry-owned agent of a cluster and returns the ids that changed. The
observation intake calls `advance_cluster` after storing a report. Nothing else activates a
revision.

It does nothing when the registry is in recovery, when the deployment is not a
registry-owned agent, when it has no revision, or when the newest revision `N` is not in
`awaiting-runtime` or `pending-approval`.

It also does nothing unless the observation of `N`'s AgentRelease is fresh and its live
generation is at least `N.generation`. Until the cluster has the object as the registry last
wrote it, what it reports is about something older.

It runs after every report, for every agent, and nearly always finds nothing to do. So it
decides twice: first on plain reads, with no transaction; and only when that says something
would change, again inside the transaction that writes it, reading through the transaction.
The second decision is the one that counts. Two replicas that take the same report then
record one event, not two.

**`N` has no approval.** `failures = gate_failures(release.document, observation.document,
pin_for(...))`.

- Failures: phase `awaiting-runtime`, `reason = failures[0]`. Event `runtime-verdict`
  (`detail.failures`) only when the phase or the reason changes.
- No failures: look for a covering approval (section 7).
  - Found: `approval_id` is set to it, `generation = seq`, phase `awaiting-runtime`, reason
    `approved; waiting for Ready`. Event `approval-carried-forward` (`detail`: approval id,
    revision).
  - Not found: phase `pending-approval`. Event `approval-needed` (`detail.reason`) only when
    the phase or the reason changes. The reason says why no approval covers it. With `A` the
    newest `active` approval of this deployment with origin `registry`:
    - there is no `A`: `no release has been approved under the registry yet`;
    - `A.policy_revision` is not the tenant's now: `the tenant policy changed since the last
      approval`;
    - otherwise the first entry of `widens(<authority of revision A.revision>, N.authority)`.

**`N` has an approval.** When the observation shows `spec.approval.approved` true and the
`Ready` condition `True`: phase `activated`, `activated_at = now`, reason `""`; and unless the
deployment is pinned, `current_revision = N` and the deployment's `generation = seq`. Event
`revision-activated`. Otherwise, when `Ready` carries a reason other than the one stored,
`reason = "waiting for Ready: <reason>"` and event `runtime-verdict`.

## 7. Which approval covers a revision

An approval `A` covers revision `N` without a person when all of these hold:

- `A` is of this deployment, `origin == "registry"` and `state == "active"`;
- `A.policy_revision` equals the tenant's policy revision now;
- `widens(<authority of revision A.revision>, N.authority) == []`.

The newest such approval is used (the last in `store.approvals(deployment)` order).

An approval imported from the pull request path, or adopted in recovery, never covers a later
revision: it records that a person approved one release under the old path, and it is bound
to nothing. So the first release recorded after authority is taken always needs a person.

**Base authority** of a revision awaiting approval: the authority of the deployment's current
revision; `{}` (`canonical.EMPTY_AUTHORITY`) when there is none.

## 8. Approve

`GET /v1/deployments/{id}/revisions/{number}/approval` is the preview: the four binding
fields (`revision`, `authorityDigest`, `baseAuthorityDigest`, `policyRevision`), the authority
and the base authority, `changes = widens(base, authority)`, `gateFailures`, the observation's
freshness, `approved`, and `approvable` (not approved, observation fresh with a live
generation of at least the revision's, no gate failure, and the revision is the newest).
`gateFailures` is `["the release has not been observed on the cluster"]` or
`["the last observation of the release is too old"]` or
`["the cluster does not have this revision yet"]` when the gates cannot be read.

`POST /v1/deployments/{id}/approvals` with `{approvalId, revision, authorityDigest,
baseAuthorityDigest, policyRevision, ticket?}`. The client reads the preview, shows it to
the person, and sends back the four fields unchanged with an id it made.

Checks after those of section 4, in this order:

| # | Refusal | When |
| --- | --- | --- |
| 1 | `replayed-approval` | an approval with this `approvalId` exists (for any deployment) |
| 2 | `not-found` | the deployment has no revision with that number |
| 3 | `stale-approval` | the revision is not the newest; or it already has an approval; or its phase is neither `awaiting-runtime` nor `pending-approval` |
| 4 | `stale-approval` | `authorityDigest` is not the revision's |
| 5 | `stale-approval` | `policyRevision` is not the tenant's policy revision now |
| 6 | `stale-approval` | `baseAuthorityDigest` is not the digest of the base authority now |
| 7 | `refused` | the release's observation is not fresh, or its live generation is below the revision's |
| 8 | `refused` | `gate_failures(...)` is not empty; the message lists every failure |

Then, in one transaction, with event `approved` (`detail`: approval id, revision, both
digests, policy revision, approver) whose sequence number is `seq`:

- the `Approval`: origin `registry`, `approved_by` the caller's email, `approved_at = now`,
  the four binding fields, the ticket, and `evidence` = `{failures: [], observedAt,
  signer: <verified subject>, attestations: [...], policyDigest, uid}` from the observation;
- every other `active` approval of the deployment with origin `registry` becomes
  `superseded`;
- the revision: `approval_id`, `generation = seq`, phase `awaiting-runtime`, reason
  `approved; waiting for Ready`.

Answer: the approval and the revision. Activation follows from `advance` when the cluster
reports the release approved and Ready.

## 9. Pin, enable, disable

`POST /v1/deployments/{id}/pin` with `{revision: n | null}`.

- `n`: the revision must exist (`not-found`), be `activated`, and its AgentRelease must be
  Ready on a fresh observation (`refused` otherwise). Then `pinned_revision = n`,
  `current_revision = n`, `generation = seq`, event `pinned`.
- `null`: `pinned_revision = None`; `current_revision` becomes the highest-numbered
  `activated` revision; `generation = seq`; event `unpinned`.
- Already so: `changed: false`, nothing written.

`POST /v1/deployments/{id}/enabled` with `{enabled, reason}`: sets `Deployment.enabled`,
`generation = seq`, event `enabled` or `disabled` (`detail.reason`). Already so:
`changed: false`, nothing written.

## 10. Cases

Each is one test in `tests/test_write_cases.py`. The starting point of every case: `db-admin`
imported with two approved revisions (pointer on 2), authority `registry`, all three objects
observed fresh.

| Case | Expect |
| --- | --- |
| Record a release with the same authority | revision 3, `awaiting-runtime`; deployment generation raised |
| Record the same release again | `created: false`, no new event |
| Record a different document under the same release name | `conflict` |
| Record a document that says it is approved | `invalid-arguments` |
| Verdict passes, first release under the registry | `pending-approval` (imported approvals never carry forward) |
| Approve it; cluster reports Ready | `activated`, pointer on 3 |
| Next release, same authority | approval carried forward, activated with no person |
| Next release, lower budget | carried forward |
| Next release, one more tool | `pending-approval`; reason names the tool |
| Next release after the tenant policy changed | `pending-approval` |
| A gate fails | stays `awaiting-runtime`; reason is the failure; approve is `refused` |
| Observation missing, old, or older than the revision | approve is `refused`; advance changes nothing |
| Approve with the wrong authority digest, base digest, policy revision, or an older revision | `stale-approval`, nothing written |
| Approve twice with the same id | `replayed-approval` |
| Approve an already approved revision with a new id | `stale-approval` |
| A newer release recorded while one waits | the older becomes `superseded` |
| Pin to revision 2, then record and approve a release | the release activates, the pointer stays on 2 |
| Unpin | the pointer moves to the highest activated revision |
| Disable, enable | `enabled` and generation change; a repeat is `changed: false` |
| Any write for an agent on the pull request path | `wrong-path`, nothing written, no event |
| Any write while in recovery | `in-recovery`, nothing written |
| A caller without the operator role, or a service account | `refused` with status 403 |

## 11. Events

`release-recorded`, `runtime-verdict`, `approval-needed`, `approval-carried-forward`,
`approved`, `revision-activated`, `pinned`, `unpinned`, `enabled`, `disabled`. All carry the
deployment id. The actor is the caller's email for a route and `registry` for `advance`.

## 12. What status says about an agent

`GET /v1/status` (package 1.3) answers, per agent, `blockers` (why it is not running what was
last published) and `nextActions` (commands a person can run). Both are built from the
records and the last observations only, in this order. An agent with nothing to say has two
empty lists.

| When | Blocker | Next action |
| --- | --- | --- |
| the registry is in recovery (registry-owned agents) | `the registry is in recovery; nothing is being changed` | |
| the Agent's observation is `missing` | `the cluster has never reported this agent` | |
| it is `old` | `the cluster last reported this agent <n> s ago` | |
| it is `absent` | `the agent is not on the cluster` | |
| the deployment is disabled | `the agent is disabled` | |
| a `projection` observation of one of its objects is fresh with result `refused-flux-inventory` | `Flux still owns <kind>/<name>` | `merge the pull request that removes <agent> from the Flux kustomization` |
| the same with `refused-older` | `the cluster holds a newer <kind>/<name> than the registry` | |
| the same with `error` | `<kind>/<name> could not be written: <message>` | |
| the newest revision is `awaiting-runtime` and has no approval | `release <name>: <reason>` | pull request path: `just agent approve <agent> <release>` |
| the newest revision is `pending-approval` | `release <name> waits for approval: <reason>` | `just agent approve <agent>` |
| the newest revision is `awaiting-runtime` and has an approval | `release <name>: <reason>` | |
| the deployment is pinned and a later revision is activated | `pinned to revision <n>; revision <m> is activated and not in use` | |
| the current release's observation is fresh and its `Ready` condition is not `True` | `current release <name> is not Ready: <reason>` | |

`ready` is the current release's `Ready` condition from a fresh observation, and null when
there is no current revision or no fresh observation. `phase`, `newestRevision` and
`newestRelease` are of the highest-numbered revision.
