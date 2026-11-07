# Bounded normal session wait and report

Extend the existing operator command `agent-session-submit` with bounded waiting, exact-run receipts, resume and report-only retrieval. Baseline: `bc248f710bcd02ebc94785df1b9c48b63f0b93cd`. This is a small operator tooling change; root reviews this document before implementation.

## Approach and scope

Use the existing Temporal runner API and `worker.connect(tenant)`. A new HTTP service would add an unnecessary authentication/deployment boundary. The separate `src/zetra` OpenShell SDK uses incompatible session and result types and is not the implementation target.

Preserve `submit(client, inp, *, rpc_timeout=None)` and its return type, existing CLI flags, governed-pool admission, and default submit-only stdout workflow ID. Add `--wait`, `--wait-timeout` (600 seconds), `--rpc-timeout` (5 seconds), `--cleanup-timeout` (120 seconds), `--cancel-on-timeout`, `--receipt FILE`, `--resume FILE`, `--report FILE`, and `--artifact-root DIR`. All timeouts must be positive finite numbers. `--report` requires `--wait` and explicit artifact root. Resume requires wait and rejects submission/task/seed/grant/pool arguments; it must never submit a new workflow.

This command runs in an already authorized operator/runtime context with externally provisioned Temporal mTLS, tenant payload codec key and access to the correct tenant artifact backing. Use existing connection environment variables; never read credentials from Pods, provision credentials, change RBAC, or introduce an insecure fallback. Explicit artifact-root access is an operator capability, not general end-user authorization. CLI changes alone require no runtime image, tag or deployment.

## Exact execution and receipts

An immutable version-1 receipt contains namespace, tenant, agent, release, session ID, worker pool, workflow ID and run ID; no task, grants, credentials or output. Emit receipt JSON to stderr immediately after accepted/bound submission; optionally create the receipt file exclusively with private permissions. Reject an existing receipt destination before submission. A receipt can be written to disk and used for resume after interruption. Receipt failure after accepted submission must still print the exact identity and return failure without resubmission.

Require `first_execution_run_id` on a successful start. Duplicate submission must use `WorkflowAlreadyStartedError.run_id`, reject absence, pin that run and compare the decoded original `SessionInput` to the complete requested input. Never return an unpinned duplicate handle. Both new and resumed operations verify namespace `agents-<tenant>`, workflow ID, canonical UUIDv4 session, canonical run UUID, workflow type `SessionWorkflow`, derived pool queue, decoded original input identity and lineage. Reject reset, retry, continuation and cron lineage. Resume binds identity from history without storing or echoing its task/grant. All subsequent calls use that exact run and `result(follow_runs=False)`.

## Bounded wait and cancellation

Use monotonic overall deadlines and bounded RPCs, including connection, governed routing, start and history inspection. Poll describe/query with a bounded interval; cap each RPC and sleep by remaining time. CLI timeout does not cancel unless explicitly requested. Return a nonzero result with the exact receipt and cleanup `NOT_CONFIRMED` when execution/cleanup proof is unavailable. Do not retry an ambiguous start automatically or redispatch model/tools.

On `--cancel-on-timeout` or first interrupt, request cancellation once against the exact run and wait up to the separate cleanup timeout. A second interrupt exits promptly with the receipt and uncertainty. Cancellation request is never deletion proof. The workflow closes cancelled after its cleanup, so inspect final query state rather than expecting a normal result. Cancelled runs remain non-success even with confirmed cleanup; terminal query failure, termination or timeout cannot establish cleanup. Do not invoke a new cleanup workflow or alter reconciler semantics.

Success requires Temporal `COMPLETED`, result outcome `COMPLETED`, query state `CLEANED`, and both cleanup dictionaries exactly `{stop: OK, delete: DELETED, grant: OK}`. Any disagreement or missing proof is non-success. Wait mode prints a compact JSON envelope with receipt, workflow status, outcome and cleanup; omit task, summary, snapshot/state contents and raw exceptions. Exit 0 only for confirmed successful completion, 2 for invalid input, 124 for local timeout, 130 for interrupt, and 1 for other failure or unconfirmed cleanup. Preserve default submit-only output.

## Report boundary

Only after successful completion and cleanup proof, resolve the snapshot key from that exact result. Accept a small injected store interface `open(key) -> BinaryIO`; the CLI adapts explicit artifact root through the existing `FilesystemStore`. Read at most 64 MiB plus one sentinel byte, verify the lowercase SHA-256 key against all archive bytes, and accept only uncompressed tar. Limit archive traversal to 256 members. Read exactly one regular, non-link member named `workspace/report.md`, at most 16,000 bytes, with strict UTF-8 decoding. Reject duplicate report members, malformed/truncated archives and short reads. Never unpack any member, accept arbitrary artifact keys/paths, return state, or expose the complete archive. Run the blocking store reader in a daemon thread with a bounded asynchronous wait; timeout/interrupt discards late completion and never joins that reader at CLI shutdown. Only the main caller may write output after a successful read. Write the requested report file exclusively with private permissions after all verification; a preexisting destination fails before submission. A missing/invalid report is failure even when execution succeeded. DB-specific evidence qualification is outside this command.

## Verification

Use fake Temporal clients/history and in-memory archives to test exact-run identity, duplicate mismatch/lineage, governed queues, resume without submission, bounded time/cancel behavior, cleanup disagreement, archive tampering and unsafe report members. Verify existing submit tests and CLI flags remain compatible. Update platform operator usage and playbook conformance. No live qualification is implied by these unit tests.
