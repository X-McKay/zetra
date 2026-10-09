---
name: zetra-deployment-qualification
description: Qualify a specific Zetra release and enforcement integration on an explicitly identified disposable or authorized Kubernetes environment, recording positive and forbidden paths. Use for deployment validation, not ordinary agent coding.
---

# Zetra deployment qualification

Read `integrations/README.md`, its validation report, `docs/deployment.html` and the versioned integration fixtures. Resolve the user's requested release, cluster, namespace and mutation scope from the conversation before choosing commands. Existing authorization persists; do not ask again for an already authorized step.

Use an explicit kubeconfig/context for every Kubernetes and Helm command. Inventory is read-only; installing a privileged node daemon or replacing CNI affects the whole cluster and requires authorization covering that change. A skill does not itself authorize installation, external publication or deletion. Prefer an isolated disposable lab when that is the user's intended environment.

Record source/image/policy/evidence digests, actual component versions, kernel/runtime/CNI and environment identity. Keep generated credentials and kubeconfigs in ignored private paths. Preserve the original environment and record any service stopped, even when authorized.

Separate discovery, successful installation, allowed-path success and forbidden-path denial. Exercise direct egress, identity mismatch/expiry, unauthorized tools, sandbox filesystem/network constraints, kernel scope, retry/replay and revocation where supported. Missing mandatory coverage, unsupported features and inconclusive proof remain failed release gates. Do not fill them with simulated events or inferred successes.

Observe effective policies and real decision events. Traffic redirection does not decrypt TLS; a tool-name permit does not authorize its arguments; an activity retry can repeat effects. Read-only Temporal success does not qualify durable writes.

For deployment generation, retain withheld activation until independent gates pass. Keep a bounded retry/cleanup plan for installs; investigate concrete failures before repeating a command. Do not broaden privileges to make an unexplained test pass.

Return a reproduction command, evidence paths and the exact passed/open matrix. A local lab can qualify only its recorded tuple; production approval and adoption of experimental upstream components are distinct decisions.
