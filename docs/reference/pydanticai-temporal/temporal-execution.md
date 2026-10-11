# 6. Temporal execution standard

Temporal durability applies only when `agent.run()` executes inside a Temporal workflow. Attaching `TemporalDurability` without starting a workflow produces an ordinary, non-durable run.

## Workflow boundary

A workflow MAY coordinate agent runs, maintain process state, wait for approval, apply deterministic branching, enforce deadlines, and initiate compensation.

A workflow MUST NOT make network, database, or filesystem calls; read mutable environment configuration during replay; use nondeterministic time or randomness; or contain infrastructure clients and credentials.

Model requests, tool calls, MCP communication, databases, external APIs, object storage, and notifications execute in activities.

## Stable identities

These identifiers MUST be explicit and stable:

- Agent name
- Toolset ID
- Workflow and activity types
- Task queue
- Signal and update names
- Durable model registry keys

PydanticAI derives activity names from agent names and toolset IDs. Renaming them can break active workflow replay.

A workflow ID SHOULD be unique and traceable:

```text
<environment>:<tenant>:<workflow-type>:<business-id>:<request-id>
```

Independent workflow chains MUST NOT reuse an ID. `Continue-As-New` remains part of the same chain.

## Activity contract

Every activity MUST define:

- Typed input and output
- Start-to-close and schedule-to-close bounds
- Retry policy and non-retryable errors
- Idempotency behavior
- Cancellation behavior
- Data-classification constraints
- Telemetry attributes

Long-running activities SHOULD heartbeat and react to cancellation.

## Retry ownership

Retries exist at four layers:

1. PydanticAI semantic correction retries
2. Provider-client transport retries
3. Temporal activity retries
4. Workflow-level business retries

The default policy is:

- PydanticAI owns semantic correction.
- Temporal owns transient infrastructure retries.
- Provider transport retries are disabled or minimized under Temporal.
- Workflows own explicit business retry decisions.

All layers MUST have a calculated combined upper bound. Authentication errors, authorization failure, policy violations, invalid business requests, and oversized payloads are non-retryable.

## Idempotency

Side-effecting activities MUST use a stable idempotency key, such as:

```text
<workflow-id>:<tool-call-id>:<operation-version>
```

The same key MUST be reused across retries. When an external system lacks idempotency support, the application SHOULD persist an operation record and check it before repeating the action.

## Timeouts

Timeouts SHOULD satisfy:

```text
provider request timeout
    < activity start-to-close timeout
        < activity schedule-to-close timeout
            < workflow deadline
                < caller or business deadline
```

Human waiting uses workflow timers, not sleeping activities.

## Human approval

A human-governed workflow:

1. Produces a typed action proposal.
2. Applies deterministic policy checks.
3. Records a canonical hash of the action and arguments.
4. Waits for an authenticated Temporal Signal or Update.
5. Verifies approver scope, expiry, tenant, actor, policy version, and action hash.
6. Executes the consequential activity with an approval ID and idempotency key.
7. Records the result and approval evidence.

Approval is invalid if material arguments or security context change.

```python
class ApprovalRecord(BaseModel):
    approval_id: str
    workflow_id: str
    action_name: str
    action_arguments_hash: str
    approver_id: str
    decision: Literal["approved", "rejected"]
    policy_version: str
    decided_at: datetime
    expires_at: datetime
    reason: str | None = None
```

## Cancellation and compensation

Workflows MUST define cancellation before, during, and after side effects. Cancellation does not automatically undo completed work. Reversible operations SHOULD have explicit compensation activities.

Final status SHOULD distinguish:

```text
completed
rejected
cancelled_without_effect
cancelled_after_partial_effect
compensated
failed_retryable_exhausted
failed_permanent
```

## Payloads and history

Temporal stores workflow and activity payloads in history and commonly enforces a 2 MB payload limit. Binary encoding further reduces usable raw size.

- Keep documents, images, large tool results, and model artifacts outside workflow history.
- Store large data in approved object storage and pass typed references.
- Keep dependencies and metadata JSON-serializable.
- Do not put secrets in inputs, dependencies, metadata, or results.
- Use `Continue-As-New` for unbounded histories.

```python
class ArtifactReference(BaseModel):
    uri: str
    media_type: str
    size_bytes: int
    sha256: str
    data_classification: str
    expires_at: datetime | None
```

The resolver MUST authorize access; possession of a URI is not authorization.

## Streaming

For durable user-facing streaming, prefer Temporal Workflow Streams where supported. Consumers MUST tolerate duplicate model events caused by activity retry and deduplicate using workflow, activity, attempt, and event identity. The durable workflow result remains authoritative.

## Deployment and replay

Before deploying a workflow change:

- Replay representative histories against the new worker.
- Verify stable agent names and toolset IDs.
- Identify changes to workflow command ordering.
- Use Temporal Worker Versioning or patching for incompatible changes.
- Retain compatible workers until older workflows finish or migrate.
- Test workflows waiting on approvals, timers, and retries.

## Required tests

- Workflow tests with skipped time
- Activity unit tests
- Retry and permanent-failure tests
- Idempotency and duplicate-execution tests
- Cancellation and compensation tests
- Approval, rejection, expiry, and tampering tests
- Payload-limit tests
- Worker shutdown and resume tests
- Captured-history replay tests
- End-to-end tests against a local Temporal server
