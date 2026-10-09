---
name: zetra-governance-review
description: Review a Zetra agent or release for risk-tier obligations, material scenario coverage, catalog evidence, exceptions and kill-switch readiness. Use for requested governance or release-readiness review.
---

# Zetra governance review

Use `docs/governance.html`, `SECURITY.md`, the agent manifest, executable scenario map, latest evidence and actual integration report. Treat manifest claims, profile observations and unsigned local evidence as distinct sources with different trust.

Classify inherent risk conservatively from data, action consequences, autonomy, exposure and blast radius. Unknown capabilities or incomplete classification require explicit review. Controls affect residual risk; they do not erase the inherent tier. Explain the triggering facts rather than inventing a numerical confidence score.

Check coverage by stable material-scenario IDs and executed outcomes. Reject stale/unbound evidence, known critical failures, missing required scenarios and unsupported required controls regardless of average quality. Do not call file presence or CRD discovery enforcement proof.

Check owner/business function, release/image/policy/evidence binding, signer trust, exception approver/scope/expiry, runtime effective-state acknowledgement and revocation status. A local catalog candidate is not a published or approved record. General enterprise guidance is not a legal compliance certification.

Review kill-switch authority across intake, workflow, gateway/credentials, backend and workload. Require measured convergence and explicit behavior for stale caches, unavailable control plane, restarts and already committed effects. A STOP file or pod deletion alone is insufficient for distributed revocation.

Produce concrete findings tied to affected evidence and a disposition: ready for the stated scope, blocked by named required controls, or requires accountable risk decision. Do not publish catalog entries, send messages or approve exceptions unless the user has authorized those actions.
