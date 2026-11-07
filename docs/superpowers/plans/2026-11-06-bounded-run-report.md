# Bounded Run Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded exact-run wait/resume and verified report-only export to the ordinary operator submission CLI.

**Architecture:** Keep the normal runner submission API and default CLI compatible. Add focused operator helpers for pinned Temporal execution and safe report extraction; reuse existing connection configuration and filesystem artifact adapter.

**Tech Stack:** Python, Temporal Python SDK 1.34.0, asyncio, tarfile, pytest.

**Spec:** `docs/superpowers/specs/2026-11-06-bounded-run-report.md`

## Global Constraints

- Preserve `submit(client, inp, *, rpc_timeout=None)` and its return type, existing CLI flags, governed-pool admission, and default submit-only stdout workflow ID.
- All subsequent calls use that exact run and `result(follow_runs=False)`.
- Use existing connection environment variables; never read credentials from Pods, provision credentials, change RBAC, or introduce an insecure fallback.
- CLI changes alone require no runtime image, tag or deployment.
- DB-specific evidence qualification is outside this command.

## Review Focus

- Ambiguous start must return uncertainty without automatic duplicate execution.
- Resume must reject identity/lineage drift and never submit.
- Governed routing and connection must obey the overall deadline.
- Cancellation with inaccessible final query must retain cleanup uncertainty.
- Existing output files must fail before submission; report archives must never be extracted.

---

### Task 1: Deliver the complete bounded operator command

**Files:**

- Modify: `platform/runner/runner/submit.py` — preserve API, pin duplicate starts, parse additive flags and orchestrate operator operations.
- Create: `platform/runner/runner/operator.py` — receipt validation, exact-run history binding, bounded inspect/wait/cancel and report-only verification.
- Create: `platform/runner/tests/test_operator.py` — identity/deadline/cleanup/archive cases.
- Create: `platform/runner/tests/test_submit_cli.py` — compatibility, parser and output behavior.
- Modify: `platform/runner/tests/test_aps_codec_submit.py` — duplicate pinned input validation regressions.
- Modify: `docs/platform.md` — operator prerequisites, examples and limits.
- Modify: `docs/playbook-conformance.md` — precise unit-tested claims and live qualification limits.

**Interfaces:**

- Consumes: `worker.connect(tenant)`, `SessionInput`, `SessionResult`, `SessionWorkflow.status`, `FilesystemStore.open(key)` and existing governed routing.
- Produces in `operator.py`: frozen `ExecutionReceipt` version-1 identity dataclass; `async bind_execution(client, receipt, *, expected_input: SessionInput | None, rpc_timeout: timedelta)` returning an exact Temporal handle after history validation; `async wait_execution(client, receipt, *, timeout: float, rpc_timeout: float, cleanup_timeout: float, cancel_on_timeout: bool) -> dict` returning the spec's sanitized envelope; `read_report(store, snapshot: str) -> str` implementing the fixed report boundary; `async read_report_bounded(store, snapshot, *, timeout) -> str` using a daemon reader so timeout/interrupt cannot join a blocked executor or write late output. Interrupt orchestration may use a small private helper around the same exact-run cancel/inspect path.
- `submit()` duplicate handling consumes the same binder with full expected input; no second identity implementation.
- CLI receipt JSON uses only the spec's receipt fields. Receipt parsing rejects unknown/missing fields and wrong types. No untrusted receipt selects a new connection tenant without validation.

- [ ] Write failing tests for missing accepted/duplicate run IDs, duplicate changed task/release/grant/pool, namespace/type/queue mismatches and reset/retry/continuation/cron lineage; assert every workflow call pins the run and results disable following runs.
- [ ] Write failing tests for resume validation/no start call, old flags/default stdout, report/receipt preexisting destinations before start, invalid timeout values and sanitized error output.
- [ ] Write failing tests using controlled clocks/fake RPCs for connection/routing/start bounds, deadline expiration, no implicit cancel, explicit cancel once, interrupt cleanup wait, second interrupt, ambiguous start without redispatch, and exact receipt preservation.
- [ ] Write failing tests for successful cleanup proof and each missing/disagreeing cleanup field; cancelled-with-cleanup remains non-success and cancelled-with-query-error remains unconfirmed.
- [ ] Write archive tests for valid report, missing/duplicate report, hash mismatch, 64 MiB bound, 256-member bound, 16,000-byte bound, links, malformed/truncated tar, strict UTF-8, short reads and no state/artifact extraction. Assert exclusive report output and private permissions.
- [ ] Run from `platform/runner` in the locked development environment: `uv run --frozen pytest tests/test_operator.py tests/test_submit_cli.py tests/test_aps_codec_submit.py -q`; confirm failures exercise missing behavior rather than unrelated environment issues.
- [ ] Implement the listed interfaces and CLI behavior, following the spec's exact defaults, proof gates, output and exit statuses. Keep activity imports lazy for artifact-root adaptation; no agent code imports or changes to workflow/worker cleanup semantics.
- [ ] Run the focused command again, then `uv run --frozen pytest -m 'not cluster' -q` and `uv run --frozen ruff check runner tests` from `platform/runner`; resolve failures attributable to this change. Run repository-required `just check` in the locked environment; report unavailable checks honestly.
- [ ] Update the two documentation files with operator credential/artifact prerequisites and submit, wait/report and resume examples. State that passing unit tests establishes no new live deployment/auth guarantee.
- [ ] Review diff against every spec section and Review Focus item; root reviews before integration. Commit only the scoped implementation/tests/docs after checks; do not tag, publish, deploy or run a live session.
