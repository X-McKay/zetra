# PydanticAI and Temporal reference

These documents moved here from the [Playbooks](https://github.com/X-McKay/playbooks) when that repository became technology-neutral. They show how the playbook standards look on one concrete stack, PydanticAI agents with Temporal durable execution, and they are the most direct bridge between the neutral guidance and Zetra's code.

| Document | Origin in the Playbooks |
| --- | --- |
| [Agent reference implementation](agent-reference-implementation.md) | Agent Playbook, chapter 12 |
| [Temporal execution standard](temporal-execution.md) | Agent Playbook, chapter 6 |
| [Multi-agent reference implementation](multi-agent-reference-implementation.md) | Multi-Agent Systems Playbook, chapter 12 |
| [Temporal durable coordination](temporal-durable-coordination.md) | Multi-Agent Systems Playbook, chapter 6 |
| [Agent references](agent-references.md) | Agent Playbook, references |
| [Multi-agent references](multi-agent-references.md) | Multi-Agent Systems Playbook, references |
| [SDK notes](sdk-notes.md) | SDK-specific passages cut from the other chapters |

They are kept as written, so relative links inside them point at their old locations and some commands name `agentctl`. Zetra's own core is framework-neutral; PydanticAI is one adapter, not a requirement. Where these documents and Zetra's code disagree, the code and its recorded evidence describe what actually exists. See the [playbook conformance map](../../playbook-conformance.md).
