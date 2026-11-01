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

## Platform guest adapter

The adapter installs SDK commit `d63867833bf64efda9c77b6f9625905b38d8cf02`
from Git into its own site-packages. The resolver rejected the old OpenAI 1.109.1
pin because the SDK's LiteLLM 1.95.0 requires OpenAI >=2.20,<3. The runtime now pins
2.54.0, matching the SDK lock; old immutable images retain their dependencies.

Initial integration: 12 passed, 5 failed. Fixture corrections fixed tuple-shaped
gateway replies and mutable request-header capture. Refusal tests also exposed
buffered library output after a framed response. Keeping stdout/stderr isolated
through interpreter exit fixed that actual protocol defect; finalizer-output
coverage was added. The focused suite then passed 17 and the expanded full suite
passed 89. Two further RED probes caught coercion of boolean protocol versions and
incomplete/string-valued Positions; strict validation fixed both.

Final complete runtime suite: **91 passed** (67.72 seconds), including all legacy
tests, fresh-process model/tool/model execution, typed tool schemas, completion,
wrong execution identities, operation mismatch, attempt fences, late output,
accepted-prefix reconstruction in a new generation, malformed provider replies,
TLS/Host/route/auth refresh, no redirects/retries, and 403/429 handling. Ruff and
format checks passed; the runtime wheel built successfully. The reference factory
in these protocol tests is a synthetic subprocess fixture; installed production
agent package checks belong to the later migration step.

During host checkpoint implementation, a further native probe demonstrated that
unmanaged subprocess creation was still permitted by the new guest adapter. The
adapter now applies the same standard process-creation audit guard as local
Temporal before importing agent code. The probe failed before the guard, then
passed with no child created. Complete runtime verification after this correction:
**92 passed** (72.83 seconds), with Ruff/format checks passing. The guard covers
cooperative Python APIs, not hostile native code; it is not a sandbox claim.
