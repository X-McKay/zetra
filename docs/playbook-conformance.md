# Playbook conformance

Zetra implements the technology-neutral Playbooks using a specific qualified
agent profile. Implementation, live observations and production requirements are
distinct.

| Area | Implementation | Qualification boundary |
| --- | --- | --- |
| Agent contract | Strict manifest, immutable release and typed registration | Static metadata checks do not execute factories |
| Tools | Shared capture/executor registration; declared workspace effects | Cooperative code; independent OpenShell policy |
| Durability | Temporal-selected progress, one-attempt effects, warm/cold reconstruction | Serial ADK profile; unknown outcomes retained |
| Session isolation | Fresh sandbox/workspace; immutable deletion confirmation | Exact Linux/OpenShell tuple; no separate tool trust domain |
| Evaluation | Fixture and sandbox modes; source/release-bound evidence | Assertions are scoped; evidence is unsigned |
| Developer workflow | One scaffold, CLI, image recipe and two agents | Installed wheel and generated-package checks |
| Policy profiling | Scoped review candidates; denied observations separate | No automatic permission grants |
| Approval and business writes | Not admitted | Receiver identity, grants and idempotency require qualification |
| Multi-agent execution | Not admitted | Separate system execution design required |
| Production lifecycle | Local owner/gateway binding and terminal reconciliation | Distributed leases, janitor and multi-host fencing remain open |

The final local run passed 139 tests; the full kind/live-vLLM run passed all 168
tests with no skips. Three benchmark tests covered bridge/SDK latency and fresh
provisioning. See [qualification](qualification.md) for exact evidence, image
identities, sample sizes and remaining production gates.
