# Projection, observation, authority and recovery

Milestone 1. This is the contract between the registry and a cluster's provisioner
(`agent_registry/api/cluster.py`, package 1.4b; `platform/provisioner/`, package 1.5), and
for the authority and recovery routes (`agent_registry/api/handover.py`, package 1.4b).

Examples that validate against the schemas: `contract/examples/desired.json` and
`contract/examples/observations.json`.

## 1. What is projected

Only agents whose deployment has authority `registry`, and only two kinds of object in the
tenant's namespace: the `Agent`, and one `AgentRelease` for **every** revision of the
deployment. The Registry Controller reads them exactly as it reads objects that came from a
pull request; it is not changed.

Nothing is ever deleted, by the registry or by the provisioner. Earlier releases stay on the
cluster.

Known limit of milestone 1: the cluster holds one Agent per agent, so when a release changes
the Agent, the controller reports policy drift on the earlier releases. Milestone 2 removes
this by compiling each release with its own declaration.

## 2. Annotations and the field manager

Every projected object carries, under `registry.agentplatform.io/`
(`config.ANNOTATION_PREFIX`):

| Annotation | Value |
| --- | --- |
| `deployment` | the deployment id |
| `cluster` | the cluster name |
| `generation` | the object's generation, as decimal text |
| `revision` | AgentRelease only: the revision number |
| `current-release` | Agent only: the release name of the current revision; absent when there is none |

The provisioner writes with server-side apply, field manager `agent-registry-provisioner`
(`config.FIELD_MANAGER`), forcing conflicts: an object it takes over was last written by Flux.

## 3. The projected objects

`rules.projection.objects_of(store, deployment) -> list[ProjectedObject]`: the Agent first,
then the AgentReleases in ascending revision number. Each `document` is complete:
`apiVersion`, `kind`, `metadata` (`name`, `namespace`, `annotations`) and `spec`.

**Agent.** `deployment.document["agent"]`, with `spec.enabled` set to
`deployment.enabled and <the document's own spec.enabled, true when absent>`.
Generation: `deployment.generation`.

The Agent resource has defaults, so the object on the cluster holds fields a manifest may
leave out. `canonical.agent_document` fills them in (`agent_registry/defaults.py`) and every
Agent document the registry keeps has passed through it. The kept `spec` is therefore the
`spec` the cluster holds, and "equals" in sections 5, 7 and 8 means equal, key for key. The
AgentRelease resource has no defaults.

**AgentRelease**, for each revision: the release's `document`, plus `spec.approval`. The CRD
requires the block, so it is always present:

| The revision's `approval_id` | `spec.approval` |
| --- | --- |
| none | `{approved: false}` |
| an approval of origin `imported` or `recovered` | `evidence["approval"]`, exactly as it was read |
| an approval of origin `registry` | `{approved: true, approvedBy: <approved_by>, approvedAt: <approved_at as RFC 3339 UTC with Z, whole seconds>, ticket: <ticket>}`; `ticket` only when there is one |

The block follows `approval_id` whatever the approval's `state`: a superseded approval still
approved the releases it was attached to. Generation: `revision.generation`.

Taking authority therefore changes nothing on the cluster but annotations: an imported
release is projected with the block it had.

## 4. `GET /v1/clusters/{cluster}/desired?lastGeneration=N`

`lastGeneration` is the highest `generation` this provisioner ever received from the
registry. It keeps the number in a ConfigMap (`registry-provisioner-state` in its own
namespace) so that it survives a restart.

- If `lastGeneration > store.generation()`, the registry has gone back in time (a restore from
  an older backup). It enters recovery (`modes.enter_recovery`, reason
  `cluster <name> has seen generation <N>; the registry is at <M>`) and answers as in
  recovery.
- `namespaces`: the namespaces of the cluster's tenants. `fluxInventories`: the cluster's.
  `fieldManager`: `config.FIELD_MANAGER`. `generation`: `store.generation()`.
- `observe`: one entry per deployment of kind `tool-server` (`subjectKind: tool-server`) and
  `model` (`subjectKind: model-engine`) on the cluster, from the deployment's `document`:
  `name` (the item), `namespace`, `workload` (with the namespace), and for a model engine
  `servedNameFrom`.
- `deployments`: one entry per registry-owned agent of the cluster, with `objects` from
  section 3. **Empty in recovery.** `namespaces` and `observe` are still given in recovery, so
  that observation continues.

## 5. What the provisioner does (package 1.5)

One pass, about every 30 seconds:

1. Read `lastGeneration` from its ConfigMap. `GET desired`. If the registry cannot be reached
   or answers with an error, **change nothing on the cluster** and try again next pass.
2. Store `max(lastGeneration, answer.generation)` in the ConfigMap.
3. Read the Flux inventories named: `Kustomization` (`kustomize.toolkit.fluxcd.io/v1`)
   `status.inventory.entries[].id`, where an id is `<namespace>_<name>_<group>_<kind>`.
4. For each object, in the order given, decide by this table. The first row that matches
   wins.

   | Condition | Result | Writes |
   | --- | --- | --- |
   | its namespace is not in `namespaces`, or its kind is neither `Agent` nor `AgentRelease` | `error` | no |
   | it is in a Flux inventory | `refused-flux-inventory` | no |
   | it is not on the cluster | `applied` | yes |
   | the live object has no `generation` annotation | `applied` (adopted) | yes |
   | live generation is higher than the desired one | `refused-older` | no |
   | live generation is lower | `applied` | yes |
   | equal, and the live `spec` and registry annotations equal the desired ones | `unchanged` | no |
   | equal, and they differ | `applied`, message `repaired: the live object had been changed` | yes |

   The first row also covers a `document` that does not name the object beside it (another
   `kind`, `name` or `namespace`, or another `apiVersion`): `error`, nothing written.

   A failed write is `error` with the message; the pass goes on to the next object. So is a
   write after which the object still differs from the desired one: server-side apply does not
   remove a field another manager owns, and reporting `applied` on every pass would hide that.

   If an inventory of step 3 cannot be read, nothing is written that pass: every object is
   `error` with the reason, because "not in a Flux inventory" is then not known.
5. Report (section 6). A failed report changes nothing; the next pass reports again.

It never deletes, never writes any other kind, and never writes outside `namespaces`.

## 6. `POST /v1/clusters/{cluster}/observations`

What the provisioner sends each pass:

- `objects`: every `Agent` and `AgentRelease` in each namespace of `namespaces`, whoever wrote
  it, each with `inFluxInventory` (`null` when the inventory could not be read: the registry
  stores it as unknown, and status shows it as unknown, never as "not in the inventory").
  `document` holds `metadata` (`name`, `namespace`, `uid`,
  `resourceVersion`, and only the annotations under the registry prefix), `spec` and `status`.
  Nothing else leaves the cluster.
- `complete`: `{subjectKind, namespace}` for each kind and namespace it listed in full.
- `subjects`: one per `observe` entry. Tool server: `{images, replicas, readyReplicas}` from
  the workload. Model engine: the same plus `servedName`, read from the ConfigMap key named.
  `present: false` when the workload is not there.
- `projections`: the result for every object of step 4.
- `lastGeneration`, `observedAt`.

What the registry does (`rules.intake.accept`), accepted in recovery too:

1. The time stored is the earlier of `observedAt` and the registry's clock.
2. `store.put_observations` with the objects (`complete_for` from `complete`), the subjects,
   one observation of kind `projection` per result (namespace of the object, name
   `<kind in lower case>/<object name>`, document `{deployment, generation, result, message}`),
   and one of kind `projection` with namespace `""` and name `report`, document
   `{lastGeneration, objects: <count>}`. The `report` observation is how the registry knows
   when a cluster last reported at all.
3. If `lastGeneration > store.generation()`: enter recovery, as in section 4.
4. In normal mode: `importer.index_observations(store, cluster, now=...)`, then
   `rules.lifecycle.advance_cluster(store, cluster, now=..., max_age_seconds=...)`.
5. Answer `{mode, generation, stored}`.

## 7. Authority

`POST /v1/deployments/{id}/authority/take`. Checks: `in-recovery`; `not-found` (no such
deployment, or not an agent). Already `registry`: `changed: false`, nothing written.

Refused with `conflict`, the message listing every reason, unless the registry's records
equal what is on the cluster:

- the observations of the Agent and of every revision's AgentRelease are fresh;
- the live Agent's `spec` equals `deployment.document["agent"]["spec"]`;
- each live AgentRelease's `spec` without `approval` equals its release's `document["spec"]`,
  and its `approval` block equals the block of section 3 (a revision with no approval: the
  live block is absent or `approved` is not true);
- there is no present AgentRelease of this agent in the namespace that is not a revision.

Being in the Flux inventory is not a refusal: the provisioner reports
`refused-flux-inventory` until the pull request that hands the objects over has merged, and
status shows it as a blocker.

Then, in one transaction with event `authority-taken` (sequence number `seq`): authority
`registry`; the deployment's `generation = seq`; every revision's `generation = seq`; and
`deployment.document["agent"]["spec"]["enabled"]` becomes true. `deployment.enabled` already
holds the live value, so what is projected does not change, and an agent taken while disabled
can be enabled with the registry's own switch. From then on the importer skips the deployment.

`POST /v1/deployments/{id}/authority/return`. Checks: `in-recovery`; `not-found`. Sets
authority `pull-request` with event `authority-returned`; already so: `changed: false`. The
answer carries the documents to commit: `agent` (the Agent of section 3 without annotations)
and `agentReleases` (every revision's AgentRelease of section 3 without annotations, in
revision order). The objects stay on the cluster. The pin and the revisions stay recorded.

## 8. Recovery

The registry is in recovery when `Meta.mode == "recovery"`. It gets there by
`agent-registry recover --reason`, by `REGISTRY_START_IN_RECOVERY`, or by itself (sections 4
and 6). In recovery nothing is projected, every write route but leave answers `in-recovery`,
observations are accepted, and nothing advances.

Recovery never rolls a cluster back. Leaving it makes the registry's records equal to what
the clusters hold, then numbers everything above what the clusters have seen.

`GET /v1/recovery` (`rules.recovery.report`). In normal mode: no differences, `complete`
true. In recovery, for each registry-owned agent, from its last observations:

| `field` | `subject` | A difference when |
| --- | --- | --- |
| `agent` | `agent/<name>` | the live `spec` without `enabled` differs from the projected one (`registry` and `cluster` are the two digests) |
| `enabled` | `agent/<name>` | the live `spec.enabled` (true when absent) differs from the projected one |
| `current-release` | `agent/<name>` | the live `current-release` annotation names another release than the current revision's |
| `release` | `agentrelease/<name>` | a present AgentRelease of this agent is not a revision (`registry` null, `cluster` the name) |
| `approval` | `agentrelease/<name>` | the live block says `approved: true` and the revision has no approval, or has one whose projected block differs |
| `generation` | either | the live generation is higher than the registry's for that object |

A registry approval the cluster does not show yet is not a difference: it is a change that
has not been applied, and it will be once recovery is left.

Differences are sorted by deployment, subject and field. `digest` is `canonical.digest` of
them as JSON. `waitingFor` lists every cluster that has registry-owned agents and whose
`report` observation is missing, not fresh, or older than `Meta.mode_changed_at`. `complete`
is true when `waitingFor` is empty.

`POST /v1/recovery/leave` with `{digest}`. Caller: an operator. `conflict` when the registry
is not in recovery, when the report is not complete, or when `digest` is not the digest now.
Otherwise, in one transaction:

1. `advance_generation` to the highest of: every live generation annotation observed, and
   every cluster's reported `lastGeneration`.
2. Event `recovery-left` (`detail`: the digest, the number adopted); its sequence number is
   `seq`.
3. For each registry-owned agent, adopt:
   - `agent`, `enabled`: `deployment.document["agent"]["spec"]` becomes the live `spec`, and
     `deployment.enabled` the live `spec.enabled` (true when absent). When `enabled` is
     adopted, the document's own `spec.enabled` becomes true, as it does when authority is
     taken (section 7).
   - `release`, in order of name: a `Release` (`derive.agent_release_record`), its
     requirements, and a `Revision` with the next number, origin `recovered`, the live Agent
     as its `agent_document`. Approved on the cluster: phase `activated`, and an approval as
     below. Not approved: phase `awaiting-runtime`, reason `recovered unapproved`.
   - `approval`: an `Approval` with id `recovered:<deployment id>@<number>`, origin
     `recovered`, `approved_by`, `approved_at`, `ticket` from the live block,
     `evidence = {"approval": <the live block>}`; the revision's `approval_id` is set to it
     and its phase is `activated`. An approval it replaces becomes `superseded`.
   - `current-release`: `current_revision` becomes the revision of the release the annotation
     names, when there is one.
   - the deployment's `generation` and every revision's `generation` become `seq`.
4. Mode `normal`.

Answer: the mode, the generation, and the differences adopted.

## 9. Cases

Package 1.5, each on the in-memory Kubernetes fake, through `main`:

| Case | Expect |
| --- | --- |
| Each row of the table in section 5 | the result and whether a write happened |
| The registry cannot be reached, or answers 500 or 401 | no write; `lastGeneration` unchanged; exit status non-zero for a single pass |
| An answer in recovery (no deployments) | no write; observations still reported |
| `lastGeneration` | stored before any write; sent on the next pass; never lowered |
| Observations | every Agent and AgentRelease of each namespace; annotations outside the registry prefix are not sent; a missing workload is `present: false` |
| A write fails | `error` for that object, the others are still applied |
| `examples/desired.json` | applies cleanly to an empty fake; a second pass is all `unchanged` |

Package 1.4b:

| Case | Expect |
| --- | --- |
| Projection of an imported agent after take | approval blocks byte-equal to the manifests; only annotations added |
| Projection of an unapproved revision | `spec.approval == {approved: false}` |
| Projection of a registry approval | the four-key block; no `ticket` key without a ticket |
| Disabled deployment | `spec.enabled: false` |
| Desired for a provisioner of another cluster | `refused`, status 403 |
| `lastGeneration` above the registry's | recovery entered; no deployments in the answer |
| Observation report | stored; projections and `report` stored; agents of the pull request path indexed; registry-owned agents advanced |
| Take with fresh equal observations | authority `registry`; generations raised |
| Take with a stale observation, a differing spec, a differing approval block, or an unknown live release | `conflict`, nothing written |
| Return | authority `pull-request`; documents equal the manifests the agent was imported from |
| Recovery report | each difference of section 8 from a prepared cluster state; `complete` false until the cluster reports |
| Leave with the wrong digest, or incomplete | `conflict` |
| Leave | records equal the cluster; generation above every live one; a second report has no differences |
| Any write route in recovery | `in-recovery` |
