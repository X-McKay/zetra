# Runtime unification validation record

This record covers the versioned shared-core integration work. It is local unsigned
engineering evidence, not release approval or cluster qualification.

## Shared SDK prerequisite

Added scoped injected MCP calls, exact offline fixtures and independent fixture
provenance in local/CLI/eval/quality/local Temporal, required tool inventory,
a model-client injection seam and shared receipt validation.

Observed RED checks: nine binding tests, three CLI/eval/Temporal tests, one quality
integration test and eight core seam tests failed before implementation. Focused
GREEN runs passed, including a real Temporal server and an HTTP model transport.
The first full run had 437 passes, 46 skips and two failures: existing receipt tests
used the removed validator test-double interface and an obsolete error/invalid
workspace fixture. Updating those test fixtures retained their off-thread heartbeat
and alternate-prefix rejection assertions. The focused regression run passed eight.

Final full SDK run with Temporal CLI 1.9.1 explicitly enabled: **439 passed,
46 skipped**, one existing ADK experimental warning (209.95 seconds). Ruff lint,
format and diff checks passed. Local Python was 3.12.13 and uv 0.12.7; repository
Python/uv pins remain unchanged. Skips do not establish enforcement.

Legacy platform baselines before adapter integration: runtime **67 passed**;
runner **125 passed**, with the installed native Temporal test server enabled.
The eleven new core subprocess/protocol tests failed before implementation because
the core protocol was not present. Subsequent integration results are recorded below
when actually observed.
