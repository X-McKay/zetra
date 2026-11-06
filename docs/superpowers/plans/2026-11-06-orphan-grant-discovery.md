# Orphan grant discovery implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep a session discoverable after stop removes its vault when deletion of the actual grant record fails.

**Architecture:** The existing reconciler API continues returning the same session fields. Both Store implementations treat either a vault or the stored grant handle's record as held cleanup state. No new credentials, authorization, mutation APIs or production timeout changes.

**Tech Stack:** Go APS Store, Memory, Redis/miniredis; existing Python cleanup reconciler.

**Spec:** Kubani `agent-runtime-platform-spec.md` sections10.1/10.7 and T24: independent cleanup converges within10 minutes and no Pod, grant or active generation remains. Parent accepted this narrow fix before T24 harness work.

## Global constraints

- Preserve confirmed Pod deletion and all #49 stopped/pending-stop semantics.
- Do not infer grant-record absence from vault absence or the existing grant HTTP404.
- Preserve API fields and caller authorization; no live cluster mutations.

## Review focus

- Successful stop plus failed grant deletion must retain the handle for retry.
- A grant without a vault is still cleanup work.
- A vault without a grant record is still cleanup work.
- Once both resources are gone, the row disappears.
- Memory and Redis expose the same state and no credential content.

### Task1: preserve orphan grant discovery

**Files:** `platform/aps/internal/store/{store.go,memory.go,redis.go,store_test.go}` and `docs/playbook-conformance.md`.

**Interfaces:** Keep `ListSessions(context.Context,time.Time) ([]SessionInfo,error)` and public JSON unchanged. `HasGrant` internally denotes held vault or grant-record state.

- [x] Add `TestStoppedSessionRetainsOrphanGrantUntilDeleted` against both backends: create generation/vault/grant/metadata, stop and remove vault, complete stop, require list row with no generation and exact grant handle; confirm actual grant remains; delete it and require empty list.
- [x] Add inverse missing-record/live-vault case; require discoverability until vault removal.
- [x] Run these tests and confirm the orphan-record case fails for both backends.
- [x] Update only the held-state checks in Memory/Redis and the internal field comment.
- [x] Run focused regressions, all APS tests and vet; document evidence and remaining live qualification gap.
- [x] Request independent review; no material findings. Commit and open a separate PR. Parent owns merging/images/live operations.
