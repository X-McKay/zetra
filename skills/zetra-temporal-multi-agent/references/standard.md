# Durable Multi-Agent Coordination Standard

The workflow owns composition manifest, graph version, state transitions, system budgets, delegation lifecycle, aggregation, approvals, cancellation, and termination. Activities own all nondeterministic execution.

Persist stable delegation IDs, attempt numbers, parent IDs, depth, requested authority, reserved budget, deadlines, and result references. Reserve budget atomically before dispatch and reconcile actual use afterward.

Use child workflows for durable units with independent lifecycle, signals, or retention needs—not for every model call. Apply finite child count, concurrency, recursion, and history limits. Continue as new only with an explicitly carried state snapshot and manifest.

An approval authorizes exactly one reviewed action at a known state. Revalidate authorization, data, budget, action hash, manifest, expiry, and prior execution immediately before the effect.

Terminal states must distinguish success, escalation, permanent failure, cancellation, compensation completed, compensation failed, and partial effect.
