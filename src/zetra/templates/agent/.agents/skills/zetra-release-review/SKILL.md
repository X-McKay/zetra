---
name: zetra-release-review
description: Use when checking a Zetra agent candidate's readiness or preparing its release evidence and review summary.
---

Read [current release and qualification boundaries](../../../docs/development.md),
the repository workflows, and the candidate's actual reports. Identify the source,
SDK/lock, prompt/tool/skill resources, model settings and artifact being reviewed.

Run applicable local validation and verify packaged resources/dependency metadata.
Inventory test/eval results, missing or skipped requirements, capability changes,
qualification evidence and provenance. Evidence for different inputs or an earlier
candidate does not qualify the proposed artifact. A checksum alone is not a signed
attestation or production authorization.

For example, a wheel with passing fixture tests and no live isolation evidence may
be prepared for review. Report it as a locally validated build with qualification
gaps, not a production-qualified release. The starter's release workflow builds a
wheel/checksum; inspect the workflow before claiming it publishes or deploys.

Return the candidate identity, validation evidence, capability changes, blockers
and next concrete action. Preserve failed/unknown outcomes. Release review does
not itself authorize a push, publication, promotion or deployment; carry out only
actions within the user's existing scope and the platform's real approval rules.
