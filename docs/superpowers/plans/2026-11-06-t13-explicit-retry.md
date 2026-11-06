# T13 explicit retry and failure evidence implementation plan

> Execute inline with the test-driven-development and verification-before-completion skills. Root owns releases and all live mutations.

**Goal:** Qualify a fresh, explicitly named daily ledger without erasing the failed run's existing ledger, and retain bounded diagnostic evidence when orchestration fails.

**Architecture:** Add exactly `conformance-budget-day-retry` alongside the existing day identity. Its signed fixture, limits and enforced backend remain identical. The runner chooses this identity explicitly; APS and the dedicated backend accept only the expanded fixed allowlist. A timestamped gateway event and retained partial observations let the external operator meet the existing deadline and diagnose failures.

**Spec:** Playbook T13/T13b budget limits and the parent-approved post-run24 retry design. This qualifies only the enforced byte-token budget path, never Qwen accounting.

- [x] Add red tests for exact retry identity selection, denial of arbitrary aliases, matching identity in every evidence source, and unchanged limits/zero baseline.
- [x] Add red tests for integer `emitted_ns`, the unchanged 100-second deadline, late-ready gateway rejection, and partial timeout evidence including ledger 1800 without raw exception/log text. Retain independent cleanup and cancellation behavior.
- [x] Implement runner identity selection, timestamped watcher receipts, safe partial error results and exact APS/backend allowlist addition. Full runner, APS/backend tests/vet and Ruff.
- [x] Copy the existing day fixture into the explicitly named locked agent project, adjust manifest/deploy/project identity, and verify repository checks and installed image validation. No release tag here.
- [ ] Stage isolated Kubani exact identity policy/RBAC/observer/backend wiring and suspended Job; signer and release assets wait for root's sequential release. Preserve original ledger, all real backend bounds, key isolation and approval gates.
- [ ] Independent review, feature PRs; root merges/releases/builds/deploys after CI. No live qualification claims from unit tests.
