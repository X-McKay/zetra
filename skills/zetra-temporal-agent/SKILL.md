---
name: zetra-temporal-agent
description: Implement or review durable PydanticAI agent execution with Temporal. Use when adding workflows, activities, retries, timeouts, idempotency, signals, human approval, continue-as-new, payload versioning, replay tests, or recovery for long-running agent work.
---

# Implement a Temporal Agent

> Zetra adapter skill. It applies the technology-neutral guidance from the [Playbooks](https://github.com/X-McKay/playbooks) to Zetra's Temporal stack. Zetra's core stays framework-neutral; use this skill only where Temporal is the selected adapter. Where it refers to `agentctl` or `scripts/agentctl`, use the equivalent `zetra` command or `just` recipe if one exists.

Use Temporal to make orchestration durable while keeping model calls and other nondeterministic work inside activities.

## Workflow

1. Confirm that the agent needs durable execution. Use `ephemeral` execution only for bounded work without externally visible side effects; use `durable` or `human_governed` execution for Temporal-backed paths.
2. Draw the workflow boundary. Put only deterministic coordination, durable state transitions, timers, signals, queries, and activity scheduling in workflow code.
3. Put model inference, PydanticAI runs, database access, network I/O, file I/O, random values, and wall-clock decisions in activities.
4. Define versioned Pydantic payloads for workflow inputs, activity inputs, results, signals, and externally stored state.
5. Assign stable business identifiers to workflows and idempotency keys to side-effecting activities.
6. Configure timeouts and retries by failure class. Never blindly retry authentication failures, invalid input, policy denial, or non-idempotent writes.
7. Model human approval as an explicit workflow state with a signal, timeout, authorized actor, and audit record.
8. Bound histories and payload sizes. Use external artifact storage and `continue_as_new` when histories can grow indefinitely.
9. Add unit, integration, time-skipping, failure-injection, idempotency, and replay tests.
10. Document deployment compatibility for in-flight workflows.

## Required Separation

```text
Temporal workflow -> schedules activity -> builds/runs PydanticAI agent -> calls allowed tools
```

Read [the durability standard](references/standard.md) before implementing workflow code. Use [the examples](references/examples.md) for retry, approval, and workflow patterns.

## Guardrails

- Do not call a model or external service from workflow code.
- Do not use unconstrained retries for side effects.
- Do not persist large transcripts in workflow history.
- Do not change workflow behavior for in-flight executions without replay testing or versioning.
- Do not treat Temporal durability as authorization.

## Completion Check

Demonstrate recovery after worker loss, safe activity retries, deterministic replay, approval timeout behavior, and trace correlation across workflow, activity, agent run, model call, and tool call.
