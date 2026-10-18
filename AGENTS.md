# Zetra contributor instructions

Zetra implements the technology-neutral Playbooks with Google ADK, Temporal and
OpenShell. Agent code runs inside a fresh sandbox per session; the external worker
owns accepted progress. Read README.md, SECURITY.md and docs/architecture.md.

Use the locked environment: `nix develop`, `just bootstrap`, `just check`. Live
qualification runs separately against the explicit `kind-zetra-validation`
context and `.state/kubeconfig`. Never change a global runtime or kube context.

Keep one implementation and public API. Update examples, installed templates,
documentation and tests together. Static commands must not import agent code.
Do not weaken profile admission, unknown-outcome handling or confirmed deletion.

Tests and measurements establish only the behavior actually exercised. Skips do
not prove enforcement; unsigned evidence is not production authorization. Never
rewrite recorded failures into passes. Preserve meaningful failures in the PR
qualification report and rerun changed boundaries.

Keep client certificates, keys, kubeconfigs and session/artifact state in ignored
paths. Do not commit model credentials or sensitive prompts. Update
`docs/playbook-conformance.md` whenever implementation or qualification changes.
