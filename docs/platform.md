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
| `platform/aps` | 9 | Agent Policy Service (Go): ext_authz, token and grants API, Redis ledger and vault, settlement |
| `platform/runner` | 10 | Session Runner, cleanup reconciler, AES-GCM payload codec |
| `platform/capture` | 15.2, 15.4 | Conformance capture proxy: records method, path, status, `x-agent-*` headers and identifying JWT claims, never the token or a body. An optional mounted static key adds only a `backend_key_match` boolean for T7. |
| `platform/runner` (`runner/conformance.py`) | 15 | Phase B conformance harness: `agent-conformance` submits conformance sessions, reads committed boundaries, APS/gateway logs and upstream captures, and writes a JSON report (T4, T5, T6, T7, T8, T11, T12, T19, T26). |

Each directory is its own project with its own lock. `just platform-check` runs
every project's lint and tests; tests that need a cluster are marked `cluster`
and are named after the conformance test they cover. Nothing here has run on a
cluster yet; the items in spec 17.3 must be confirmed there before Phase A is
accepted. Deployment manifests live in Kubani.

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
