# Agent Runtime Platform

`platform/` implements the Agent Runtime Platform specification (revision 2):
Kata sandboxes reachable only through agentgateway, OAuth agent identity from a
projected ServiceAccount token, the Agent Policy Service as fail-closed external
authorization, and a Session Runner that drives one boundary per kubelet exec.
It is built alongside the OpenShell runtime in `src/zetra`; that path stays
supported until the platform's Phase B conformance tests pass on a cluster.

| Directory | Spec section | Contents |
| --- | --- | --- |
| `platform/contracts` | 3, 5.2, 5.3, 6.3 | `agent_platform` constants and naming, policy bundle and digest, pod template, tenant NetworkPolicy set and digest, `agentctl` |
| `../deploy/platform/crds` | 6.1, 6.2 | `Agent` and `AgentRelease` CRDs (vendored by Kubani) |
| `platform/controller` | 6.4, 7.3, 7.4 | Registry Controller (kopf) with a Keycloak admin adapter |
| `platform/netpol-attestor` | 5.5 | node DaemonSet recording host-side kube-router chains on sandbox pods |
| `platform/egress-guard` | 5.5 | sandbox init container; `--attack` mode for T2b |
| `platform/agent-runtime` | 11 | `agent_runtime` sandbox SDK baked into agent images |
| `platform/agent-tools` | 14.1 | `zetra-agents`: the layout every agent repository shares (`agents/<name>/` members of one uv workspace, one lock, one rendered Dockerfile), `check`, `render`, `init`, offline `preflight`, release artifacts and attestation predicates; `.github/actions/agent-release` holds the keyless release steps repositories call |
| `platform/aps` | 9 | Agent Policy Service (Go): ext_authz, token and grants API, Redis ledger and vault, settlement |
| `platform/runner` | 10 | Session Runner, cleanup reconciler, AES-GCM payload codec |
| `platform/capture` | 15.2, 15.4 | Conformance capture proxy: records method, path, status, `x-agent-*` headers and identifying JWT claims, never the token or a body. An optional mounted static key adds only a `backend_key_match` boolean for T7. |
| `platform/runner` (`runner/conformance.py`) | 15 | Phase B conformance harness: `agent-conformance` submits conformance sessions, reads committed boundaries, APS/gateway logs and upstream captures, and writes a JSON report (T4, T5, T6, T7, T8, T11, T12, T19, T26). |

Each directory is its own project with its own lock. `just platform-check` runs
every project's lint and tests; tests that need a cluster are marked `cluster`
and are named after the conformance test they cover. Nothing here has run on a
cluster yet; the items in spec 17.3 must be confirmed there before Phase A is
accepted. Deployment manifests live in Kubani.

## Platform images

`platform/images.json` lists every image repository built from `platform/`: its
Dockerfile, build context, target platform and build arguments, and optionally
`inputs` (see "Releasing platform images"). The `images` job in
`.github/workflows/platform.yml` reads that file and builds each entry with
`docker buildx build --pull --no-cache`. A passing job means every image
builds from that commit. The job does not check the pins: the `contracts` tests
(`platform/contracts/tests/test_images.py`) fail when a `FROM` in a
`platform/*/Dockerfile` names an image without a tag and a digest, or when a
build argument in `images.json` is not declared by its Dockerfile.

The `images` job pushes and signs nothing. Builds are not bit-reproducible:
netpol-attestor's `apt-get install`, for one, takes whatever Debian serves that day. The
`conformance-capture` entry assumes that repository is `platform/capture` built
for amd64; no build record confirms it.

Each `FROM` keeps its tag and adds the digest of the multi-platform index, not
of one platform's manifest, so the cross-compiled and arm64 builds resolve the
right base. To refresh a digest, run
`docker buildx imagetools inspect <image>:<tag>`, take the top-level `Digest`
and replace it in every `platform/*/Dockerfile` that names the image. The
Dockerfiles outside `platform/` are not pinned this way. `COSIGN_SHA256` in
`images.json` is the SHA-256 of the `cosign-linux-amd64` release binary for
`COSIGN_VERSION`; change the two together, hashing the binary you downloaded.
`platform/controller/Dockerfile` keeps a default for `COSIGN_VERSION`; the same
tests fail when it differs from `images.json`.

### Releasing platform images

`.github/workflows/platform-release.yml` builds and pushes the images that a
merge to `main` changed. Until it has run once on the cluster's runner, treat
every statement here as the design and not as something observed; the images
Kubani deploys so far were built and pushed by hand.

- **What is released.** On a push to `main`, the `changes` job runs
  `platform/contracts/agent_platform/images.py changed <before> <after>`. An
  image is released when the push changed its Dockerfile, a file under its build
  context, or its entry in `images.json`. An entry may carry `inputs`, a list of
  repository paths; the build context is then not used for change detection, a
  file under one of the `inputs` counts instead, and a test requires every
  source of every `COPY` and `ADD` in the Dockerfile to lie under them, so the
  list cannot be narrower than what the build reads. `registry-controller` uses
  it, because its context is `platform`. An image whose inputs did not change is
  never built; if none changed, the run ends successfully having built nothing.
  When the push's "before" commit is all zeros or unknown to the checkout the
  run releases nothing and says why. A release therefore cannot be re-run
  except by a new merge that changes an input.
- **How.** The `release` job runs on the self-hosted runner scale set
  `kubani-zetra`, one image at a time (`max-parallel: 1`; no `concurrency`
  group, because GitHub keeps one waiting run per group and could drop a
  release). The composite action `.github/actions/platform-release/` refuses to
  continue unless the image is an entry in `images.json`, the commit being built
  is the pushed commit and an ancestor of `origin/main`, and the checkout is
  clean; it then builds with the flags of the pull request job and pushes.
- **The tag.** `registry.almckay.io/platform/<image>:<first seven characters of
  the commit>`; the full reference with its digest goes to the job summary and
  to the action's `reference` output. An existing tag is never overwritten: if
  it exists the job fails and names the digest.
- **The credential.** The workflow handles no password. The cluster mounts a
  docker client configuration for `registry.almckay.io` into the runner, and the
  action fails with a message if it is missing.
- **The guard on the runner.** The runner refuses any job unless the workflow
  reference is exactly
  `X-McKay/zetra/.github/workflows/platform-release.yml@refs/heads/main`, the
  event is `push` and the ref is `refs/heads/main`. That is why the workflow has
  no other trigger (not even `workflow_dispatch`) and why every job that needs
  the runner lives in this file.
- **Nothing is deployed.** A merge builds and pushes; a platform image reaches
  the cluster only when a pin changes in the infrastructure repository.
- **Not done yet.** The images are not signed, carry no attestations, and
  admission does not check them (the plan's Phases 2 and 3). The workflow
  already requests the identity-token permission that signing will use.

## Token qualification (T4/T5)

T4 reads the existing `probe_files` tool result from the hash-verified final
committed snapshot. The runtime token cache itself is excluded from snapshots;
only its decoded, allowlisted claims are consumed. Passing requires both
`agent-gateway` and `agent-policy-service` audiences, `client_type: agent`, the
exact configured tenant/agent, a 300-second `exp - iat`, the exact `ISSUER_URL`,
and a JTI matching APS issuance for the final sandbox generation. Model prose
cannot substitute for these claims, and unreadable APS logs fail the gate.

T5 retains the six sandbox refusal probes and the non-sandbox issuer-audience
case. Direct cases run before the sandbox sessions so those sessions do not
consume a short-lived human fixture's lifetime. Its API-audience case uses `API_TOKEN_PATH`, defaulting to
`/var/run/secrets/kubernetes.io/serviceaccount/token`. It first authenticates
that exact token to the Kubernetes SelfSubjectReview API and verifies its
unexpired, pod-bound ServiceAccount claims. `T5_API_SUBJECT` defaults to
`system:serviceaccount:agent-platform:conformance-harness`. An APS-audience
token is not accepted as a substitute.

The human case requires a genuine unexpired Keycloak access token mounted
read-only at `T5_HUMAN_TOKEN_FILE`. Set `T5_HUMAN_SUBJECT` and
`T5_HUMAN_USERNAME` to the expected human identity; `T5_HUMAN_CLIENT_ID`
defaults to `agent-cli`. The harness verifies RS256 against the configured
issuer's signing keys, exact issuer/identity/client, access-token type and
lifetime, and rejects agent, service-account or delegated tokens. `T5_HUMAN_JWKS_URL`
defaults to `${ISSUER_URL}/protocol/openid-connect/certs`; an explicit internal
Keycloak URL is supported for the trusted harness. No bearer is sent to JWKS,
redirects are refused, and an unavailable key source fails without fallback.
GitOps owns the narrow API/Keycloak network access and read-only fixture mount.

Both the API and human assertions must receive exactly 401 `invalid_client`
from the APS token route. Missing, expired, invalid or unverifiable fixtures
are explicit errors, so T5 cannot pass in full without the human fixture. Only
allowlisted claims, statuses, fixed error labels and error classes enter the
report; raw assertions and arbitrary response bodies do not. Earlier eight-case
T5 passes and issuance-only T4 passes retain their narrower historical meaning.

## Model authentication qualification (T6/T7)

Select `agent-conformance --release RELEASE --tests T6,T7` from the harness Job
in `agent-platform`. T6 requires `T6_MODEL_URL` to be the full direct engine URL
(for example `http://llm-engine.vllm.svc.cluster.local:8000/v1/models`) and
`BACKEND_KEY_FILE` to name a read-only file containing the raw engine key. The
harness adds the Bearer scheme, makes an unauthenticated request followed by an
authenticated request, and requires 401 then 200. It does not follow redirects
or retain response bodies. Configure the temporary NetworkPolicy exception at
this harness vantage point; sandbox network denial does not establish T6.

T7 runs a normal conformance session through the runner and reads the `capture`
container on `app=vllm` pods in namespace `vllm`, separately from MCP capture.
The model capture sidecar must have `CAPTURE_BACKEND_KEY_FILE` pointing to a
read-only copy of the same raw key the gateway presents. A configured missing,
empty or invalid file stops startup. Omit the variable to retain the existing
MCP capture behavior. The proxy compares the incoming bearer locally and emits
only `backend_key_match`, never a key or key hash.
The capture Dockerfile cross-compiles on the builder's native CPU; select
`docker build --platform=linux/arm64 platform/capture` for the main vLLM node,
or publish an image index containing every deployment architecture.

Passing T7 requires session-correlated POST `/v1/chat/completions` records with
status 200, a matching backend key and no JWT `jti`; the static bearer must be
reported as `not_a_jwt`. The gateway must record an accepted agent identity on
`/models/llm-main/v1/chat/completions`, and both committed model boundaries and
gateway access logs must contain nonzero token usage. Missing capture, identity
or usage evidence fails the test. The harness ServiceAccount needs pod/log read
access in `vllm`; the GitOps deployment owns that RBAC and the key mounts.

## Cache-separation qualification (T19)

T19 requires a conformance release with the structured `probe_mcp` evidence
interface. One tool boundary pins one agent token and sends 50 consecutive
allowed/forbidden MCP cycles. The harness requires 100 HTTP statuses alternating
200/403, 100 distinct APS decisions with matching tools/reasons and one token
`jti`, and a measured MCP interval no greater than 10,000 ms. It correlates the
requests using trace ids rather than relying on log ordering across APS replicas.

The same probe then performs two main/fast model-route cycles. Each main request
uses the admitted model and must return 200; each fast request uses that route's
correct model name and must return 403 `deny.route_not_permitted`. Six-second
pauses before, between and after these cycles allow the preceding model's budget
reservation to settle. The model half has no ten-second limit. There are no
retries that hide 429s or other failures; two cycles keep the explicit main calls
plus the driver's two model boundaries within the existing burst of five.

The harness reads the exact probe result from `state/state.json` in the final
accepted snapshot, verifies the archive hash and bounds the member size. It
does not grade the model's truncated summary. Missing, ambiguous or malformed
snapshot evidence fails the gate. Earlier five-cycle T19 runs remain evidence
only for the narrower behavior they exercised.

## Keyless signing (private Sigstore)

The Registry Controller has three signature profiles, chosen with
`REGISTRY_SIGNATURE_PROFILE`:

| Profile | Verifies |
| --- | --- |
| `homelab` | `cosign verify --key` on the image |
| `enterprise` | the above, plus a release attestation carrying `manifestSha256` and `policySha256` |
| `keyless` | a Fulcio certificate from the private Sigstore (`REGISTRY_SIGSTORE_TRUSTED_ROOT`, optional `REGISTRY_SIGSTORE_REKOR_URL`, and `SIGSTORE_ROOT_FILE`, `SIGSTORE_REKOR_PUBLIC_KEY` and `SIGSTORE_CT_LOG_PUBLIC_KEY_FILE` with the same roots for attestations) whose issuer and subject match the tenant namespace's declaration, plus every attestation the namespace requires, signed by the same identity |

The namespace declaration is a set of annotations, read by both the controller
and Kyverno (`agent_platform.signing`):

| Annotation | Meaning |
| --- | --- |
| `agentplatform.io/signer-issuer` | OIDC issuer, normally `https://token.actions.githubusercontent.com` |
| `agentplatform.io/signer-subject-regexp` | anchored regexp for the certificate subject (the release workflow on a tag) |
| `agentplatform.io/required-attestations` | comma-separated predicate types; default SLSA provenance, SPDX SBOM, cosign vuln, `https://agentplatform.io/eval/v1` and the release predicate (which binds `manifestSha256` and `policySha256`) |
| `agentplatform.io/vuln-exceptions` | comma-separated vulnerability ids allowed at high severity; critical is never waivable |

`agentctl signer-annotations --repo <owner/name> [--agent <name> ...]` prints
the annotations for a repo's `release.yml`. Pass `--agent` whenever one repo
releases agents for more than one tenant: without it the declaration accepts
every agent's release tag in that repo. Independently, the controller requires
the certificate's tag (`<agent>/v<version>`) to name `AgentRelease.spec.agent`
(`SignerAgentMismatch` otherwise). Every attestation of a required type must
pass, so a later failing scan or eval on the same digest is never masked by an
earlier clean one. A missing or unanchored declaration, or one with a top-level
`|`, fails closed. The
verified identity and attestation types are written to
`AgentRelease.status.verifiedSigner`.

The eval predicate is
`{"suite", "commit" (40-hex), "passed": true, "total" >= 1, "failed": 0, "results"?: [{"name", "passed"}]}`.

## agentctl validate

`agentctl validate --agent agent.yaml --release release.yaml --mcp-resource-base https://mcp.<domain>`
runs the release checks without a cluster: digest pinning, field formats,
the policy bundle compile and its digest, and the sandbox pod and NetworkPolicy
render (`--out DIR` writes them). `--eval` and `--vuln` check local predicate
JSON with the same conditions the controller applies. `--policies DIR` runs
`kyverno apply` on the rendered pod; give it the shape policies only, since an
unsigned local image cannot pass the verifyImages rules.

## Active-session revocation qualification (T15/T20)

Run these tests inside the `agent-platform` harness Job with
`--tests T15,T20 --revocation-fixture-release RELEASE --allow-revocation-mutations`.
The fixture is always the separate `conformance-b` Agent, whose signed release
must include `probe_revocation`. No existing conformance-b sandbox may be active.
The primary agent remains available for the other tests.

The probe holds one token and issues bounded MCP initialize requests. The harness
joins actual gateway HTTP responses to APS decisions by decision ID, session,
pod and probe trace prefix. Both Ready APS replicas must provide baseline allows
and later 403s. Model summaries, transport failures and pod disappearance are
never denial evidence. T15 repeats disable/re-enable/cancel against the same two
warm replicas. T20 adds a tool, checks controller-produced drift/not-ready within
ten seconds, unchanged policy/ConfigMaps, active-session denial, real preflight
refusal and a CRD-rejected server-side dry-run image edit.

The operator precreates the temporary namespaced Kyverno Policy
`tenant-test/conformance-revocation-delete-hold` with the inert rule returned by
`runner.conformance_revocation.hold_rule()`. The harness may patch only this
Policy and the fixture Agent/release by resource name. It temporarily holds
DELETE of one exact test Pod name and UID. A Kyverno blocked PolicyViolation
Event must identify the rule and related Pod UID. Real Temporal cancellation
still runs normal `stop_session`; deletion is deliberately denied so the running
sandbox can observe revocation. The policy is restored under shield before the
existing trusted cleanup reconciler removes any remaining pod.

Timing is conservative: the disable bound runs from mutation request-start to
observed gateway 403 and includes log delivery. Cancellation-to-observed-403
latency is recorded as informational; T15's one-second gate runs from the actual
earliest actual APS stop event to a real gateway 403 decision on that same replica, with one
millisecond added for timestamp truncation. Both Ready replicas must return real
403s. Sampling continues until a valid same-replica stop/denial pair is available,
including when the first decision's truncated timestamp precedes the stop. Such
an ambiguous pair is rejected; a later pair must still satisfy the original
one-second bound. Later idempotent stops cannot reset this clock. This avoids cross-node clock skew and does not count Temporal
cancellation scheduling or log delivery against the stop-to-denial bound.

Restoration of the Agent spec, its per-resource Flux reconciliation annotation,
inert Policy rule, original policy digest/ConfigMaps and pod cleanup are gates.
A fresh successful session is also required. The operator independently removes
the temporary Policy and RBAC after any run, including Job failure. The harness
has no Pod mutation/exec, secret-read, status-write, or cluster-policy permission.
These tests require new signed probe and runner images; unit tests are not live
qualification results.

## Operator session wait and report

`agent-session-submit` uses the existing Temporal runner API. Run it only in an
already authorized operator context with the tenant's externally provisioned
`TEMPORAL_TLS_CERT`, `TEMPORAL_TLS_KEY`, optional CA/server-name configuration and
`PAYLOAD_KEY_FILE`. `TEMPORAL_ADDRESS` must reach the intended frontend;
`TEMPORAL_NAMESPACE`, if set, must equal `agents-<tenant>`. Do not copy credentials
from running Pods. This is not a general end-user login or authorization service.

Existing submit-only flags and workflow-ID stdout remain supported. The command
also emits an exact execution receipt on stderr. Save it before waiting:

```sh
agent-session-submit --tenant tenant-test --agent db-admin \
  --release db-admin-reviewed --task-file task.json \
  --receipt session-receipt.json --wait --wait-timeout 600 --rpc-timeout 5
```

Wait mode prints compact JSON containing exact identity, workflow status, outcome
and cleanup certainty. Success requires successful execution and matching confirmed
cleanup from the exact run. Every call pins the run; resume never submits work:

```sh
agent-session-submit --resume session-receipt.json --wait \
  --report report.md --artifact-root /operator/tenant-test-artifacts
```

Report export requires existing access to the correct tenant artifact backing and
an explicit local root. Only verified `workspace/report.md` is returned; the archive
and state are never extracted. Receipt and report files are created exclusively
with mode 0600; existing destinations, including dangling links, are refused.
Generic export does not qualify DB-specific facts or evidence links.

`--wait-timeout` bounds submission plus waiting (default 600 seconds),
`--rpc-timeout` bounds connection/submission operations and individual RPCs (5),
and `--cleanup-timeout` bounds a cancellation cleanup wait (120). All must be
positive and finite. A local timeout leaves the workflow running unless
`--cancel-on-timeout` is supplied. First interrupt requests exact-run cancellation
and waits for cleanup; a second interrupt exits with uncertainty. Cancellation
alone does not prove deletion. Exit codes: 0 confirmed success, 1 execution/report
failure or unconfirmed cleanup, 2 invalid input, 124 local timeout, 130 interrupt.
An ambiguous start is never automatically retried; preserve its failed invocation
and resolve its identity before deciding whether to submit again.

These operator CLI changes require no runner image rollout or new RBAC.
