# Starter activation and local Temporal validation — 2026-10-31

The published local-development SDK foundation is
`e9bdc66fb52a4f1c0d713b2cfb30e467afc7ec94`. Canonical starter commit `a0a06b9`
activated that pin and lock, V2 quality cases, pre-build gates and developer skills
pack 0.1.1. A fresh rendered environment installed that actual Git revision from
site-packages: 30 tests, all 6 quality requirements (9 mapped tests, 1 fixture
case), lint/format and wheel checks passed. Canonical tests: 13 passed. Both
repositories' published GitHub checks passed before the Temporal increment.

## Local Temporal implementation

The adapter uses unchanged `SessionWorkflow` and `Activities`, plus the shared
`Runtime`, `Definition`, `Dispatcher`, `Tape` and provider converter. The only
production-runtime seam is an optional objects directory with the previous
default retained. Production workers, Release admission, OpenShell settings and
retry policies are unchanged. A separate strict local request avoids inventing
an image/release for host source.

Cold children reconstruct from accepted history at each boundary. Native tests
observe two model execute Activities and one tool execute Activity for the
starter, and replay that history. Shared immutable artifacts retain candidates
without making them accepted. Retained leases, generations, terminal tombstones
and pre-effect attempt markers prevent overlapping writers, late admission and
redispatch after an ambiguous completion. Children use Linux parent-death handling;
normal process-creation APIs are blocked in the cooperative in-process profile.

Qualification used the official [Temporal CLI 1.9.1 release](https://github.com/temporalio/cli/releases/tag/v1.9.1),
reporting Server 1.32.0 / UI 2.54.1. The Linux amd64 archive was checked against
the release checksums: `09a0326a51db84d02735e53542b9ebd8c4758daf47482a9ab0abce15844e60d5`.
The new SDK CI job pins this checksum and runs the real local-server tests.
Python 3.12.13 / uv 0.12.7 were available locally; repository CI pins 3.12.14 /
0.13.1 remain unchanged. Regenerating the starter lock with available uv changed
its schema revision from 5 to 3; other dependency records were unchanged.

## Exercised boundaries

- Real scaffolded project, actual model/tool Activities, accepted tape and history replay.
- Fresh worker/router after an accepted tool result and full local server restart
  using retained SQLite history; the accepted tool is not executed again.
- Real worker-process SIGKILL during a tool: the direct child stops, the accepted
  cursor remains unchanged, the replacement reports unknown outcome, and cleanup
  completes without redispatch.
- Candidate receipt followed by lost completion: exactly one effect execution,
  candidate cursor ahead of accepted history, unknown result retained.
- Native cancellation, CLI SIGTERM and cancellation during owned-server startup;
  supported runner/workspace cleanup and owned-server shutdown are checked.
- Synthetic HTTP model endpoint through the real provider converter and tool;
  usage is recorded and the configured API-key value is absent from Workflow
  payload bytes and the result report.
- Closed-before-import and stale-generation rejection, durable duplicate-attempt
  refusal, lock retention during cancellation, failed removal retaining the
  terminal fence, source changes and interpreter-finalizer changes, inventory
  changes and process-spawning rejection.

## Failures retained and corrected

- Initial implementation tests failed because the adapter did not yet exist.
- A pause/recovery test queried before the initial position existed; the test now
  waits through that legitimate startup state. Later harness corrections fixed
  an inserted test block, a missing import and a typed client-configuration access.
- A real cancellation was incorrectly labeled `FAILED`: the client wraps its
  cancellation in `WorkflowFailureError.cause`. Classifying the cause fixed the
  native cancellation regression.
- The HTTP-model test rejected the first operation because unvalidated numeric
  defaults changed `20`/`0` into `20.0`/`0.0` after process serialization, changing
  the configuration hash. Validating defaults makes identity stable. The explicit
  digest round-trip test failed before the fix and passed afterward.
- Independent review reproduced CLI SIGTERM leaving its owned server alive.
  The real CLI regression failed with exit `-15` before the fix, then passed with
  graceful cancellation, a retained canceled report and no live owned children.
- Independent review identified source-integrity failure overwriting
  `OUTCOME_UNKNOWN` with `FAILED`. A tool that performed an effect then changed
  source reproduced it. Uncertainty/cancellation now remain intact with separate
  integrity evidence; the native regression passed after the correction.
  The same regression also failed in the default no-Temporal runner; its matching
  correction preserves the unknown outcome while recording the integrity error.
- A source mutation from an interpreter finalizer initially bypassed child-side
  checks. The host now checks metadata again after confirmed child exit and before
  returning a receipt; the failing regression passed after this fix.

The pre-review full run passed 414 tests with 46 skipped and one existing ADK
experimental-feature warning. The reviewed Temporal fixes passed a subsequent
417-test run with the same skips and warning. An installed wheel independently
scaffolded a project, passed its active quality contract and completed a real
local-server run with `SUCCEEDED` and confirmed `DELETED` cleanup.

After the matching no-Temporal integrity correction, the final full SDK suite
passed **418 tests, with 46 skipped and one existing ADK warning** in 201.90
seconds. The explicit local Temporal CLI environment variable enabled the native
tests. Ruff, formatting (175 files), documentation links, canonical bundle
provenance and the final wheel build passed. The 46 skips remain unqualified
environment-dependent coverage, not successful enforcement evidence.

## Final starter pin and installed validation

Temporal-capable SDK revision `c128bb199c64386e2fbda224e500ef40451eadcb` was
published and independently verified reachable through GitHub before changing
the starter. Canonical template revision
`e7417ee595d89e1d3093531dfe9ba93320919eea` pins that exact revision and updates
the lock and local-mode documentation. All non-SDK locked package records and
the ordinary Python factory are unchanged. The SDK bundle records this canonical
revision and its tree digest in `src/zetra/templates/provenance.json`.

A fresh canonical render installed the published Git revision into site-packages
with no caller PYTHONPATH substitution. Distribution `direct_url.json` confirmed
both the requested and installed revision. Canonical tests passed 13/13; the
rendered `just validate` passed all 30 tests, developer-skill consistency,
lint/format, all 6 quality requirements (9 mapped test items and 1 fixture case),
and wheel/resource/dependency checks. Default `dev` succeeded with three steps.
Installed `dev --temporal` with explicit CLI 1.9.1 also succeeded with three
steps and `DELETED` cleanup. Its private retained state was outside the project.
These fixture runs establish plumbing and lifecycle behavior, not model accuracy.

After the final canonical synchronization, all 18 scaffold/renderer tests passed;
provenance, docs and lint/format checks passed again. The final wheel was installed
in a separate environment, generated a starter with the exact SDK pin and active
quality contract, and passed that contract. Runtime code is identical to the
418-test SDK revision. Final head GitHub check outcomes are recorded on the paired
draft PRs after publication.

## Limits and design decisions

This is Linux trusted-host development with cooperative in-process tools. The
audit guard is not isolation against hostile Python/native syscalls; unmanaged
threads/background work are unsupported. Cold reconstruction adds import/replay
overhead. Native tests do not qualify arbitrary subprocess tools, OpenShell,
machine/power-loss durability or production deployment. Forced CLI SIGKILL can
leave its owned server running; retained state must be inspected before removal.
Private state is deliberately retained for exact-handle recovery and contains
history, tool data and immutable artifacts.

Local Temporal is exposed through `dev` and the Python development API. Fixture
and explicit local-model eval/quality modes remain available; they do not become
Temporal or sandbox qualification. No real-model accuracy study, calibrated judge,
held-out statistical trial or deployed policy attestation is claimed.

Publication uses feature branches and draft PRs, preserving pinned commit
reachability. No merge, tag, cluster change or production rollout is performed.
