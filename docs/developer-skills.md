# Developer skill pack delivery

The canonical agent template now includes seven repository-local skills for Claude
Code and Codex: agent development, tool authoring, runtime-skill authoring,
evaluation, debugging, optimization and release review. The SDK imports the same
tree through its existing canonical-template bundler; `zetra init` includes it.

See the [design and wider platform roadmap](superpowers/specs/2026-10-31-developer-skills-design.md),
[implementation plan](superpowers/plans/2026-10-31-developer-skills.md), and
[generated repository workflow](../src/zetra/templates/agent/docs/development.md).

## Ownership and use

Edit canonical content in zetra-template/template/.agents/skills. The generated
Claude copies are checked against a versioned inventory. `--sync` is an explicit
local operation that refuses conflicting mirror edits and unsafe paths before
writing; it preserves other skill names. No global assistant configuration,
permission overrides, hooks or runtime dependencies are installed.

The template's `just validate`, pytest suite and validation workflow run the
consistency check. The application wheel checker rejects developer-assistant
configuration, while the SDK wheel intentionally retains the complete template.
Runtime skills are ordinary packaged agent resources with an explicit consumer;
they remain separate from this developer pack.

The starter's SDK dependency remains pinned to
`af2e7542b89265eaa4572a586719a67e58238e74`. The pack describes that installed
contract, which admits only workspace reads/writes. A newer Zetra checkout's
`remote_read` support does not change the starter's compatibility. Updates must
review the SDK, lockfile, guidance and runtime qualification together.

## Validation record — 2026-10-31

- Canonical template suite: 13 passed.
- Pack synchronization and negative wheel tests: 19 passed.
- Freshly rendered application against its unchanged pinned SDK: 23 passed;
  lint/format checks, static metadata and the single deterministic fixture passed.
- SDK scaffold suite: 18 passed, including renamed output and deliberate mirror drift.
- Application wheel built, passed resource/dependency/developer-asset checks, and
  installed into a fresh environment with dependency resolution enabled. The
  installed factory and packaged instructions were imported outside the source tree.
- SDK wheel built, retained seven skills in each assistant location, and successfully
  scaffolded a checked project from an extracted wheel rather than the source checkout.
- All seven canonical skills passed metadata validation. An independent reviewer
  checked both renderers, references, managed-file conflicts, symlinks, packaging and
  canonical provenance. The sole material finding was stale `remote_read` advice;
  it was reproduced against the installed pin, corrected and added as a smoke case.

The local interpreter was Python 3.12.13 and uv 0.12.7. Repository CI specifies
Python 3.12.14 and uv 0.13.1; those exact tool versions were not available locally.
Dependency pins and lockfiles were not changed. These local results do not claim
execution of the hosted CI job or live platform qualification.

The first full SDK run inside the Codex tool sandbox encountered replay subprocess
timeouts and was interrupted. A focused reproduction also timed out. The same
five replay tests passed outside that tool sandbox without changing test/runtime
code. The complete local SDK suite then finished with **286 passed, 46 skipped,
1 warning in 111.09 seconds**. The 46 skips are opt-in cluster/live tests
(confirmed by marker collection); they were not counted as qualification.
A redundant sandboxed scaffold rerun was interrupted after the full outside-sandbox
run completed; the earlier focused scaffold run passed all 18 cases.

## Behavioral observations

One delegated GPT-6-luna baseline used the unmodified template, and a separate
guided sample used the pack. Both recognized the main evidence/authorization
boundaries; these samples do not establish a measured improvement.

The guided sample implemented a second approved document in a disposable rendered
repository: 24 tests and static metadata passed. Its first fixture evaluation
timed out inside the tool sandbox. Parent-assisted rerunning outside it passed.
Review also found a missing formatting blank line; the sample then corrected it
and passed lint, format and 24 tests. Preserve those initial misses rather than
describing the sample as autonomous end-to-end success. The remote-read case was
a prompted follow-up after review, not a blinded holdout.

The [baseline](evidence/developer-skills-2026-10-31/baseline.md),
[guided sample](evidence/developer-skills-2026-10-31/guided-smoke.md), and
[independent review](evidence/developer-skills-2026-10-31/review.md) preserve the
observations, including the review finding before its correction.

The versioned scenario inventory is under the generated repository's
`tests/development/skill-scenarios.json`. Repeated trials in the actual Claude Code
and Codex harnesses, automatic discovery tests, calibrated scoring, and comparative
time/token measurements remain future validation. No paid assistant benchmark,
live model evaluation, cluster qualification or production deployment ran here.

## Current integration and remaining milestones

The local runner, optional local Temporal, explicit offline MCP bindings and
machine-readable component quality gates are now implemented. The versioned
platform core profile and two reference-agent migrations extend that same
factory/runtime contract; see [platform integration](platform.md) and its
[validation record](evidence/runtime-unification-2026-10-31.md).

The [statistical eval framework](statistical-evals.md) now provides fixed repeated
trials, confidence bounds and comparison. Actual model evidence depends on explicit
provider runs. Exact-artifact production promotion, fleet telemetry, continuous
optimization, cluster qualification and zetra-ui remain separate roadmap milestones. The skill pack's behavioral observations above do not become
claims of assistant effectiveness merely because runtime features were added.
