# 6. Durable coordination standard

This chapter extends the Agent Playbook's Temporal execution requirements from
one agent run to a coordinated system. Temporal provides durability; it does not
provide authorization, safe delegation, convergence, or bounded autonomy by
itself.

## Workflow boundary

A durable system has one authoritative workflow chain for each system run. The
workflow MAY:

- Maintain typed system and branch state
- Materialize an allowed run graph
- Schedule member-agent activities and child workflows
- Reserve and reconcile budgets
- Apply deterministic routing, aggregation, and conflict policies
- Wait for signals, updates, timers, and human approval
- Propagate cancellation and initiate compensation
- Commit terminal outcomes

The workflow MUST NOT call models, agents, tools, MCP servers, registries,
databases, queues, filesystems, clocks, random generators, or external services
directly. Nondeterministic discovery and admission evidence are obtained through
activities; the workflow applies a deterministic policy to their recorded
results.

## Stable identities

These identifiers MUST be explicit and stable:

```text
system name and version
composition and run manifest IDs
system run and workflow IDs
workflow and activity types
member agent names, versions, and manifest IDs
role and interaction IDs
delegation, branch, task, event, and approval IDs
task queues, signals, updates, and durable registry keys
```

Suggested workflow and delegation identities are:

```text
workflow:   <environment>:<tenant>:<system>:<business-id>:<request-id>
delegation: <workflow-id>:<interaction-id>:<logical-task-id>
branch:     <delegation-id>:<branch-index>
```

Retries of the same logical work reuse the delegation and branch identities.
Materially changed work receives a new identity linked to its predecessor.

## Run graph and manifest

A static system starts from its resolved composition manifest. A dynamic system
also produces a run manifest containing every admitted member, role assignment,
edge, policy decision, schema, capability ceiling, and budget grant used in that
run.

Graph changes occur only through a versioned workflow command or update:

1. An agent, operator, event, or policy proposes a member or edge.
2. An activity obtains authenticated registry and compatibility evidence.
3. Deterministic workflow code evaluates the admission policy.
4. The workflow records the decision and updated graph version.
5. Only then may the new member or edge receive work.

The workflow MUST NOT depend on a registry returning the same answer during
replay. The recorded evidence and policy version make the decision reproducible.

## Typed state machine

System state MUST be represented by versioned, serializable types. At minimum,
track:

- System status and graph version
- Active, completed, failed, and cancelled delegations
- Branch parentage and dependencies
- Accepted evidence and unresolved conflicts
- Budget reservations and measured consumption
- Approval and side-effect state
- Progress markers and termination counters
- Compensation obligations

Agent prose is not workflow state until parsed, validated, and accepted through
a deterministic transition. Illegal transitions fail closed and emit policy
telemetry.

## Scheduling and concurrency

The workflow owns scheduling. It enforces:

- Maximum active members and branches
- Per-role and per-member concurrency
- Tenant and deployment capacity limits
- Dependency ordering and required barriers
- Fairness and priority policy
- Deadline and queue-time limits
- Backpressure behavior

Fan-out reserves capacity and budget before scheduling branches. If the full
fan-out cannot be admitted, policy determines whether to queue, reduce the set,
use a tested degraded mode, or escalate. The model does not silently choose a
cheaper or smaller execution plan after policy denial.

Concurrent results are assigned stable logical order independent of completion
timing. Aggregation MUST NOT depend on nondeterministic iteration or arrival
order unless arrival order is explicitly captured as an input to a versioned
business policy.

## Interaction execution

Each member invocation executes as an activity or within an explicitly managed
child workflow. Its input contains the validated delegation envelope and its
output contains the validated result envelope.

An interaction activity MUST define:

- Start-to-close and schedule-to-close timeouts
- Retry and non-retryable failure classes
- Cancellation and heartbeat behavior
- Idempotency and duplicate-detection behavior
- Data classification and payload bounds
- Budget grant and usage reporting
- Trace and audit attributes

An agent may request further delegation, but the request returns to the workflow
or another deterministic control boundary for validation and scheduling. Nested
in-process agent calls MUST NOT bypass graph, budget, authority, telemetry, or
durability controls.

## Retry ownership

Retries may exist at model, provider, agent, interaction, activity, workflow,
and business-process layers. The system documents one owner for each failure
class and calculates the combined maximum executions, duration, and cost.

The default is:

- Agent frameworks own bounded semantic correction.
- Temporal activities own transient infrastructure retry.
- Workflows own interaction and business retry decisions.
- Provider transport retries are disabled or minimized beneath Temporal.

Authorization failure, invalid admission, policy denial, incompatible schema,
invalid approval, exhausted hard budget, and invalid business input are
non-retryable. A member's `failed_retryable` status is a claim evaluated against
workflow policy, not permission to retry itself.

## Idempotency and deduplication

The stable delegation ID is the idempotency root for one logical interaction.
Side-effecting operations additionally use the Agent Playbook's stable tool-call
and operation-version identity.

The workflow deduplicates:

- Repeated delegation requests
- Retried activity results
- Duplicate queue or event delivery
- Repeated approval updates
- Late results from cancelled branches
- Replayed side-effect completion notifications

Deduplication MUST preserve the original result and audit evidence. It MUST NOT
erase inconsistent duplicate payloads; mismatches trigger an integrity finding.

## Budget reservation and reconciliation

Root budgets are durable state. Before dispatch, the workflow atomically
reserves the child grant and relevant concurrency slot. Nested work reserves
from its parent's remaining grant.

After completion or termination, the workflow:

1. Validates reported usage against provider and tool evidence where available.
2. Commits measured consumption.
3. Releases unused reservation.
4. Records unknown cost separately from zero cost.
5. Applies warning, degradation, escalation, or termination policy.

A workflow MUST tolerate worker loss between reservation, execution, usage
recording, and release without double-spending or leaking reservations.

## Progress and termination

Progress is a typed material change such as new verified evidence, completion of
a required task, resolution of a conflict, or an approved state transition.
Restating a plan, producing another critique, or transferring ownership without
new evidence does not count by default.

The workflow checks termination after every interaction and material event. It
terminates or escalates on terminal state, hard limit, deadline, cancellation,
kill switch, policy dead end, or configured no-progress window.

Persistent collectives MAY continue across business tasks, but each task has a
bounded workflow or workflow segment with its own identity, budget, outcome, and
audit record. Persistence is not permission for an unbounded model conversation
or history.

## Failure propagation and degraded operation

Every role and interaction is classified as required, optional, substitutable,
or advisory. Policy defines the effect of its failure.

Final state MUST distinguish at least:

```text
completed
completed_degraded
needs_information
escalated
rejected
cancelled_without_effect
cancelled_after_partial_effect
compensated
partial_effect
failed_retryable_exhausted
failed_permanent
```

A degraded outcome identifies missing members, evidence, checks, and guarantees.
The system MUST NOT relabel degraded completion as ordinary success solely to
meet availability objectives.

## Cancellation and compensation

Cancellation propagates from the root through active descendants using stable
branch identities. The workflow defines behavior:

- Before any side effect
- While agents or tools are active
- During approval waiting
- After partial or completed side effects
- While a dynamic member is unreachable

Completed work is not automatically undone. Reversible effects have explicit
compensation activities and ordering. Consequential effects retain approval and
audit evidence even when later compensated.

## Child workflows

Use a child workflow when a branch needs one or more of:

- An independent durable lifecycle
- Separate worker or organizational ownership
- Independent cancellation or parent-close policy
- History isolation or scaling
- Reusable coordination with its own state machine

Do not create one child workflow per agent merely for code organization. Parent
and child exchange typed summaries and artifact references rather than shared
local state. Parent-close and cancellation behavior MUST be explicit.

## Human approval

Approval binds to the final typed action and the system context that produced
it, including system and manifest ID, workflow, actor, tenant, policy version,
arguments, relevant evidence references, and expiry.

Changing a material action field, effective authority, member composition,
tenant, or governing policy invalidates approval unless the approval policy
explicitly declares the change immaterial. An agent review, vote, consensus, or
coordinator recommendation cannot approve an action.

## Payloads and history

Keep model transcripts, documents, code, images, large result sets, blackboard
contents, and event bodies outside workflow history. Store them in approved
artifact systems and pass typed, authorized, integrity-protected references.

Use `Continue-As-New` or bounded child workflows for long-lived systems. Before
continuing, persist the current graph version, run manifest, budgets, unresolved
work, approvals, progress counters, and artifact references in a versioned
continuation payload.

## Deployment and replay

Before deployment:

- Replay static and dynamic graph histories.
- Test histories with active, failed, cancelled, and late branches.
- Verify stable system, member, role, interaction, workflow, and activity IDs.
- Verify graph and policy decisions do not call mutable registries during replay.
- Test in-flight approvals, budget reservations, timers, and substitutions.
- Use Worker Versioning or patching for incompatible changes.
- Retain compatible workers until pinned executions complete or migrate.

## Required tests

- Workflow tests with skipped time
- Static and dynamic graph materialization tests
- Admission and graph-version replay tests
- Concurrent fan-out and deterministic aggregation tests
- Combined retry-bound and duplicate-delivery tests
- Budget reservation, reconciliation, and worker-loss tests
- Required, optional, fallback, and quarantined-member tests
- Deadlock, livelock, no-progress, and termination tests
- Root and branch cancellation tests
- Partial-effect and compensation tests
- Approval, expiry, tampering, and composition-change tests
- Payload, history, and `Continue-As-New` tests
- Captured-history replay tests
- End-to-end tests against a development Temporal environment
