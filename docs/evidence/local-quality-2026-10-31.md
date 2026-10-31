# Local runner and quality gate validation — 2026-10-31

This increment implements the shared no-Temporal driver, explicit local model
configuration, typed evaluation assertions, and repository-owned quality
requirements. It preserves production provider defaults and one-attempt effect
handling. It does not implement local Temporal or migrate `platform/agent-runtime`.

## Environment and dependency boundary

Validation used Python 3.12.13 and uv 0.12.7 with the existing SDK lockfile.
Repository CI requests Python 3.12.14 and uv 0.13.1; those exact tools were not
available locally. Their pins were not changed. Commands that started without an
explicit available interpreter failed on the missing 3.12.14; successful build
and lock checks explicitly selected 3.12.13. Subprocess/loopback checks ran outside
the tool sandbox after sandboxed child execution stalled earlier focused checks.

Canonical template commit: `7b1a41e`. Its SDK dependency remains
`af2e7542b89265eaa4572a586719a67e58238e74`, with the unchanged lock and active CI.
The SDK bundle matches that canonical tree and renderer provenance. Prepared
quality files are inactive under `docs/quality-upgrade/` until publication and a
validated SDK pin/lock upgrade. No remote publication or production changes were
performed by this validation.

## Exercised checks

| Check | Observation |
| --- | --- |
| Full SDK regression suite | `python -m pytest -q`: 384 passed, 46 skipped, 1 existing ADK experimental-feature warning, in 118.12 seconds |
| SDK Ruff | Lint and format checks passed for src, tests, scripts, benchmarks and examples |
| SDK lock | `uv lock --check` passed with the available interpreter |
| Canonical template | 13 tests passed; lint and format checks passed |
| Fresh rendered starter, existing installed SDK pin | 30 tests passed; static check and legacy fixture evaluation passed; developer-pack consistency, lint and format passed |
| Rendered starter wheel | Built wheel; resource bytes, exact dependency pin and exclusion of developer assets verified |
| Candidate-source rehearsal | Fresh scaffold with prepared cases activated only in the disposable project; 30 tests passed; quality passed 6 requirements with 9 mapped pytest instances and 1 case |
| Workspace reporter example | 5 tests passed; quality passed 9 requirements with 5 mapped tests and 1 outcome/trajectory/step-budget case |
| Document reader example | 1 test and fixture evaluation passed |
| Installed candidate SDK wheel | Installed into a separate environment from exported locked dependencies; verified import from site-packages, scaffolded a fresh project, activated prepared cases there, quality passed 6 requirements/9 mapped tests/1 case, and dev returned SUCCEEDED with model–tool–model events |
| Independent review | Initial P1 false pass fixed; follow-up found no remaining actionable findings; 9 focused review checks passed |

The wheel environment initially could not install completely offline because some
locked dependencies were absent from the cache. Fetching those exact locked
dependencies succeeded. No dependency changes were made to complete the check.

The synthetic loopback HTTP provider test uses the actual provider converter,
selects `read_document`, observes the returned tool value on the next request,
returns final text and checks token usage. It supplies an unreachable Temporal
address and requires no Temporal connection. Unknown tool errors remain unknown
with one observed attempt. Other checks cover explicit endpoint settings,
credential-error redaction, output overlap, symlinked input rejection, complete
passed/failed/error accounting, prevalidation, changed inputs, tool inventory,
skips/xfails/xpasses, teardown failure and filtered parametrizations.

## Failures retained and corrected

- The first example quality run failed every pytest mapping despite five passing
  tests: enclosing SDK pytest configuration changed node IDs. Explicit project
  root and conftest boundaries fixed it; a nested-project regression passed.
- Independent review reproduced a P1 false pass: `PYTEST_ADDOPTS='-k one'`
  deselected a failing parameter while a whole-function requirement passed.
  Evidence now retains pre-selection and deselected IDs. Environment and project
  filters fail generic requirements; explicit single-parameter requirements keep
  their narrower scope. RED/GREEN regressions and review verification passed.
- Reports initially retained pytest stdout/stderr and some exception messages.
  They now retain phase evidence and error types. Secret-printing and exception
  regressions demonstrated the failure before the fix and passed afterward.
- Local config errors initially echoed an inline credential; output overlap
  checks initially missed normalized parent segments; input workspace aliases
  initially followed a symlink. Each regression failed before correction and
  passed afterward.
- The first rendered `quality_starter` project failed formatting because package
  name expansion lengthened existing template lines. Canonical formatting was
  repaired; fresh ordinary and maximum-length-name render checks passed. The
  final ordinary render completed all its checks against the unchanged SDK pin.

## Scope

Fixtures establish deterministic execution plumbing, tool allowlists and the
specific failure behavior asserted. The synthetic provider establishes transport
and shared-core integration, not real model quality, production latency or cost.
Quality labels are not proofs of semantic coverage. Runtime skill declarations
are explicit resources without independently observed skill activation inventory.
Reports are unsigned and may contain sensitive tool/output/file text. No trusted
release attestations or externally enforced policy floor are supplied here.

Cluster/live qualification, model-quality datasets, repeated statistical trials,
judge calibration, token/cost gates, local Temporal, platform migration and starter
CI activation after publication remain separate work. Existing historical live
qualification records are unchanged.
