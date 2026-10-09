# Governance skill forward test

Review performed 8 October 2026 (America/New_York), using `skills/zetra-governance-review/SKILL.md` against `examples/action` as a **production** release candidate. No agent, evidence or live infrastructure was changed during this review. Findings below describe the observed snapshot; later evidence regeneration does not erase the other production gaps.

**Disposition: blocked for production promotion.** The example remains useful as a local, deterministic approval and idempotency demonstration. Its declared internal-data, supervised `ticket.write` capability establishes a T2 inherent-risk floor and requires accountable risk review; business impact and blast radius are not sufficiently specified to lower or fully finalize that classification.

| Finding | Evidence and implication | Required resolution |
| --- | --- | --- |
| Existing local evidence is stale | `/tmp/zetra-action-evidence.json` records seven executed passing scenarios, but source hash `3f24499a…` differs from current `bc7f74d9…`; its evaluator hash also differs. Actual `validate_evidence` raised `ContractError: Evidence is stale: source or manifest hash mismatch`. Passing historical outcomes cannot clear the current candidate. | Regenerate and independently verify evidence after source/evaluator changes, then bind it to the reviewed release. |
| Production authority and receiver scope are absent | `ActionStore` atomically binds actor string, payload digest, expiry, single-use approval and receipt within one SQLite transaction. The agent receives a local store; actor strings are not authenticated workload/reviewer identity. Tenant/resource/release/policy binding, independent issuance, revocation and a remote idempotent receiver are absent. The real Temporal worker explicitly refuses `ticket.write`. | Qualify an independent authenticated approval service and receiver with exact scope, durable idempotency, fencing and revocation before enabling writes. |
| No approved, signed deployment/catalog tuple exists | Manifest owner is `service-team@example.invalid`; local evidence is explicitly unsigned. No independently approved production catalog record, signer verification, image/policy digest binding or effective-state acknowledgement for this action agent was supplied. Renderer output starts at zero replicas. | Establish accountable ownership and risk decision, trusted signed release evidence and policy, catalog publication/admission verification and recorded effective state. |
| Distributed stop and write-path qualification are missing | `stop.enforced` tests only a cooperative STOP file. It does not demonstrate authority convergence through intake, Temporal, gateway, credentials and backend, or behavior for an already committed effect. Local Gateway/Temporal/collector reports concern a different, read-only status agent and explicitly exclude production write semantics, TLS identity and revocation. | Exercise production-shaped write retry/restart, revocation/fencing and measured kill-switch convergence on the exact proposed deployment tuple. |

The checked action scenario map covers `schema.contract`, `input.untrusted`, `capability.denied`, `budget.enforced`, `stop.enforced`, `approval.bound` and `idempotency.retry`. Test names were not treated as evidence: the historical artifact includes executed unittest outcomes, and source/evaluator binding was checked independently. Their scope is local SQLite and cooperative runtime controls.

Observed integration reports were `integrations/gateway-live-output/gateway-report.json`, `temporal-gateway-report.json`, `telemetry-report.json`, and `integrations/evidence/temporal-local-test.json`. They support a real read-only reference-agent → local Temporal test server → native AgentGateway → deterministic MCP receiver chain and fixture telemetry correlation. They do not transfer production approval to the action example. `integrations/evidence/toolkit-cluster-discovery.json` is explicitly discovery-only and establishes no enforcement claim.

## Skill guidance observations

The governance skill produced a defensible, scope-limited disposition: it explicitly distinguishes declarations, observations and unsigned evidence; requires executed material scenarios and independent binding; forbids treating CRD discovery as enforcement; and requires measured distributed revocation. No guidance defect causing false production approval was found in this forward test.

The companion `skills/zetra-deployment-qualification/SKILL.md` correctly preserves existing user authorization, requires explicit environment/mutation scope, identifies the cluster-wide effect of privileged daemon/CNI changes, and treats a skill as guidance rather than authorization. It explicitly separates discovery, installation, allowed-path success and forbidden-path denial, and states that read-only Temporal success does not qualify writes. Its withheld-activation rule is consistent with the toolkit renderer.

**One actionable documentation defect at this snapshot:** the deployment skill begins by requiring `integrations/README.md`, but that file did not exist when read. Add the referenced integration reproduction/status index or update the skill to the actual report index. This is a missing entry-point document, not evidence of failed or successful enforcement. Other referenced playbooks and security guidance were present.

Reproduction of the non-mutating evidence check:

```python
from pathlib import Path
from zetra.evidence import read_json, validate_evidence
from zetra.manifest import load

root = Path("examples/action").resolve()
validate_evidence(root, load(root), read_json(Path("/tmp/zetra-action-evidence.json")))
```

The evidence artifact path is machine-local and ephemeral. A reviewer needs the supplied artifact or a newly generated artifact for their own snapshot; production approval additionally requires trusted signatures and the missing controls above.
