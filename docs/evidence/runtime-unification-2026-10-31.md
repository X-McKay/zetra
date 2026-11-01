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

## Platform host integration

Core releases now bind their profile into the signed policy input and immutable
release schema. Legacy policy bytes remain unchanged. The runner accepts only
fully validated, durably persisted checkpoints; execute loss and checkpoint
exhaustion never redispatch the effect. Core results carry Position, reason and
final text; legacy result payloads and recorded Activity order remain supported.

Final affected suites: contracts **57 passed**, controller **69 passed**, runner
**195 passed**, including native Temporal execution/replay, cancellation, complete
legacy completed/replacement/refusal histories and complete core histories.
The new controller test initially failed because its fixture computed a legacy
policy digest before adding the profile; constructing the correct signed input
fixed it. A separate test proves profile/digest drift is refused.

Archive collection and restore share one worker memory slot, with heartbeats while
waiting. A concurrency regression failed before this bound and passed after it.
New histories skip cleanup before session activation; a Temporal patch preserves
old refusal-history cleanup on replay. Kubani's CRD receives only the optional
immutable profile field, preserving its newer verifiedSigner schema. Local
Kustomize rendering is not cluster admission or resource-capacity qualification.

## Reference agents and canonical starter

Static release admission: twelve new tests failed before implementation, then all
**19 tools tests passed**. They cover unsupported versions/profiles, duplicate keys,
missing/mismatched resources, tool/quality drift and symlinks without importing
agent factories. An early synthetic Agent fixture omitted its required identity;
fixing that fixture produced the final green run.

Migrated investigator: **19 tests passed**; document assistant: **17 tests passed**.
Both full component quality gates passed, including per-tool contract/security/
recovery and outcome/trajectory/step-budget evidence. The document assistant also
maps runtime-skill contract/activation/nonactivation. Its exact-excerpt tool bounds
output mechanically; fixture success is not semantic model accuracy or adversarial
prompt-injection qualification. Investigator's reviewed MCP capabilities did not
change. Repository `just check` passed with the quality gates wired in.

Both agents completed native local Temporal runs with `SUCCEEDED` and `DELETED`
cleanup. Exact installed dependency metadata confirms SDK `d638678`, runtime
`1393cf0`, and tooling contracts `bba8b09`. Both Docker images built with the repo's
Python 3.12.14/uv 0.13.1 versions and actual installed distributions.

Image fixture development first found a wrong harness property name, then a wrong
local result comparison. The synthetic MCP server also lacked `tools/list`, which
the actual MCP client requests during its call. These were harness errors, fixed
without weakening the runtime. Final image parity results are recorded below.

Canonical starter: **30 tests**, **13 renderer tests**, full quality gate and wheel
resource checks passed at SDK `d638678`. A freshly rendered `runtime-starter` also
passed `just validate` end to end. Updated bundle/provenance checks: **18 passed**.
The final guest-runtime wheel includes the process-creation guard. Local host
checks used Python 3.12.13/uv 0.12.7; Docker used the unchanged repository pins.

Final image parity passed in read-only UID-1500 containers with external network
disabled: investigator **9 boundaries / 3 exact MCP calls**, document assistant
**5 boundaries / 0 MCP calls**. Installed source/resource bytes match the reviewed
project. Cold platform commands validate receipts before advancing the test cursor;
final tool order, workspace files and text match the offline local run. Runner
checkpoint persistence/recovery is covered separately by its native tests.
