# Security boundaries

Zetra v0.1 is a development foundation, not a certified production security platform. The playbooks define the target controls; implementation and live test status are recorded explicitly.

- `check` scans source without importing it. Static hints are incomplete. `eval`, `run` and Temporal activities execute trusted agent/project code; run untrusted code only in a separately qualified sandbox.
- Runtime capability, budget and STOP-file checks are cooperative. Python code can bypass them. OS/network/backend controls must independently enforce production authority.
- Local evidence JSON and catalog candidates are unsigned and editable. Production admission must validate trusted signer identities, source/image/policy/evidence binding, freshness, approvals and revocation.
- Rendered Deployments start at zero replicas. Complete environment binding, actual worker image entrypoint, schema validation and negative enforcement tests are required before activation. No renderer command installs or approves platform services.
- The SQLite receiver demonstrates atomic approval consumption, effect and receipt within one local database transaction. Shared-process actor strings are not authenticated identity. A production receiver must enforce tenant/resource/action/release/policy scope, authentication, revocation and remote idempotency.
- Recorded profiles propose behavior for review. They never grant permissions automatically or prove evaluation completeness. Raw vendor telemetry needs a version-qualified parser.
- Actual Gateway, OpenShell, Tetragon, CNI and Temporal features vary by release, kernel and deployment. Read integration results before relying on a capability. OpenShell Kubernetes deployment is experimental upstream.
- The local lab uses disposable fixtures and test identities; it does not qualify production TLS, vault credentials, distributed kill switches or arbitrary framework durability unless those exact tests are recorded.

Do not commit kubeconfigs, private keys, raw prompts, sensitive traces, database files or local tooling. `.tools/`, `.zetra/`, `build/` and private integration artifacts are ignored.

Security fixes require a regression case for the violated invariant and clear evidence of the affected trust boundary. Until a reporting channel is established, report issues privately to the repository owner; do not publish credentials or sensitive evidence in an issue.
