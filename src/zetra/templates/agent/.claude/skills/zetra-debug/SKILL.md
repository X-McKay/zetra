---
name: zetra-debug
description: Use when reproducing or diagnosing a failed Zetra run, tool invocation, trajectory, or evaluation case.
---

Read [runner and evidence boundaries](../../../docs/development.md). Capture the
case ID, source/model/settings, runner mode and first observable failing boundary.
Use the exact handle for durable runs; avoid copying credentials or raw sensitive
traces into tracked fixtures.

Build the smallest local reproduction using synthetic/redacted inputs and explicit
tool/model doubles where needed. Distinguish agent logic, model choice, transport,
authorization, evaluator failure and missing infrastructure. A local fixture
reproduction does not certify the original distributed failure.

For a timed-out remote effect, preserve its unknown outcome. Do not repeat it to
obtain a green trace. Inspect the supported operation/history/reconciliation path
within existing authorization. Likewise, missing Temporal infrastructure cannot
be silently replaced with fixture mode in a required qualification run.

Fix the cause and add a regression at the affected boundary. Re-run the original
case where feasible and report what remains unverified. Preserve the initial
failure and its evidence; do not edit golden outputs or thresholds to conceal it.
