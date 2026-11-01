---
name: zetra-optimize
description: Use when reducing a Zetra agent's token usage, cost, or latency, or improving accuracy against a measured baseline.
---

Read [current measurement limits](../../../docs/development.md). Establish the
quality floor, optimization target and available execution budget from the task
and repository. Start with existing representative data and a reproducible baseline.

Keep dataset/holdouts, runner, grader and tool fixtures fixed across a comparison.
Record any deliberate candidate model/configuration change. Change one relevant factor, such as prompt length, context selection,
tool-result size or model configuration. Repeated paired cases reveal regressions
and variability better than a single successful run.

For a requested 30% token reduction, measure actual input/output tokens per case,
quality outcomes, errors, tool calls and latency distributions. Report the aggregate
and important failing slices. A shorter prompt or a deterministic fixture response
does not establish a 30% reduction in end-to-end model usage.

Retain a candidate only when measured quality and safety requirements still hold.
Preserve holdouts and graders; do not reduce checks, permissions or required evidence
to improve a score. If the current provider/harness cannot report a metric, name
that measurement gap and the smallest needed instrumentation. Do not run an
unbounded paid search or change live model routing as part of a local experiment.

Use the named fixed-sample experiments and `zetra eval-compare` described in
[live evaluations](../../../docs/evaluation.md). Keep plan, dataset, evaluator and
MCP identities compatible; source/model changes are explicit comparison variables.
Noninferiority can remain inconclusive even when both runs pass. Plan a new study
before collecting more samples; do not turn repeated looks into retry-to-pass.
Token caps are observational after a bounded trial, not a hard monetary limit.
