# Live gateway, Temporal, MCP, and telemetry validation

Validated on October 8, 2026, Eastern time. This is an actual integration run with deliberately narrow scope, not a production qualification.

The chain was:

```
framework-neutral gateway-read reference agent
  → actual Temporal Python worker + SDK test server (host)
  → actual agentgateway v1.6.0 (host loopback)
  → deterministic Streamable HTTP MCP fixture (host loopback)
  → agentgateway OTLP/gRPC export
  → loopback kubectl port-forward
  → actual OpenTelemetry Collector v0.162.0 (disposable kind cluster)
```

Gateway source revision: `ea5608642b9d5c94c5baf5fd9f2f9849b808e963`. The official Darwin ARM64 release binary SHA256 was checked before execution: `6cebe8bd57262edce23a65a376d2f5a27b97cdfef609740642edbe75e1e9c685`.

Collector namespace: `zetra-gateway-test` in dedicated `kind-zetra-validation`, accessed exclusively through the repository's `.tools/zetra-kubeconfig`. The official ARM64 collector image resolved to `docker.io/otel/opentelemetry-collector@sha256:310a800ad69ee430e7c541796852a242c9c7db97aaad4daa5ccf843c525fbdb2`, now pinned in the fixture manifest. Actual pod identity is retained in the evidence.

## What passed

The proxy rejected missing tokens, wrong issuer, wrong audience, forged signatures, and a token expired by one hour. A valid synthetic RS256 identity initialized an MCP session. Tool discovery returned only `read_status`. The read returned the receiver's actual `status: healthy`. The forbidden write and wrong-subject read were rejected before forwarding; the independent fixture recorded zero writes.

The actual `examples/gateway-read` factory ran in a Temporal activity. A transient failure injected before the first call caused attempts `[1, 2]`; the second produced the MCP result. The fetched workflow history replayed successfully. This uses the example's local package contract with explicit test-harness Temporal orchestration; it does not promote its manifest to a production durable release.

The cluster collector received gateway request and child spans. The explicitly injected W3C trace ID was preserved. Custom gateway span attributes contained the same release, Temporal run ID, and operation ID as the adapter's audit event. A denied write appeared in telemetry. The captured synthetic logs contained no raw JWT. These are fixture-specific assertions, not generic DLP assurance. Correlation headers are metadata; JWT verification and authorization establish identity and tool permission.

## Material observation

The initial test used a token expired by exactly 60 seconds. The real proxy accepted it. This is retained in `gateway-initial-expiry-observation.json`; the subsequent expiry test used a token expired by one hour and passed. The pinned implementation constructs `jsonwebtoken::Validation` with its defaults and does not expose a clock-leeway setting through `JWTValidationOptions`. Include measured token leeway in authority-freshness objectives; short nominal expiry is not a complete revocation mechanism. No tight revocation bound was qualified here.

## Evidence

- `gateway-live-output/gateway-report.json`: all 11 live gateway/chain assertions passed.
- `gateway-live-output/temporal-gateway-report.json`: actual reference agent result, attempts, correlation, and test-server hash.
- `gateway-live-output/temporal-gateway-history.json`: recorded history used for replay.
- `gateway-live-output/telemetry-report.json`: collector linkage and captured-log assertions.
- `gateway-live-output/collector.log`, `gateway-live-output/gateway.log`: actual outputs.
- `gateway-live-output/collector-pods.json`: runtime image identity.

The test server is the Temporal SDK test server downloaded for Python SDK `1.34.0`; it is not a full production Temporal deployment. The SDK emitted a warning about the test server not advertising the newest activity-heartbeat failure capability. This test does not exercise heartbeat preservation.

## Reproduce

Use an authorized disposable kind cluster named `zetra-validation`, its dedicated repository kubeconfig, macOS ARM64, OpenSSL, `kubectl`, and the project's Python environment with its Temporal optional dependency. Run:

```bash
bash integrations/gateway-reproduce.sh
```

The script verifies the official pinned gateway binary, applies only the collector fixture in its dedicated namespace, creates a loopback-only port-forward, runs the actual chain, captures collector evidence, and asserts delivery. It stops its port-forward on exit. It leaves the collector namespace available for inspection. Remove only that fixture when finished:

```bash
kubectl --kubeconfig "$PWD/.tools/zetra-kubeconfig" \
  --context kind-zetra-validation delete namespace zetra-gateway-test
```

Each harness run generates a synthetic private signing key under ignored `.tools/gateway-test-state`, never emits it, and deletes it at normal completion. The gateway receives only public JWKS. No paid model or external business tool is called.

## Remaining qualification

The test does not qualify TLS, a production identity issuer, production revocation/fencing, tenant/resource authorization, sandbox and direct-network bypass prevention, paid-model quality/cost accounting, Kubernetes agentgateway controller/CRD behavior, all components running in Kubernetes, or a single shared Temporal-to-gateway tracing DAG. The control contracts in the deployment and governance playbooks remain the production acceptance criteria.

Official configuration sources: [agentgateway JWT authentication](https://agentgateway.dev/docs/standalone/latest/documentation/configuration/security/jwt-authn/), [MCP authorization](https://agentgateway.dev/docs/standalone/latest/documentation/configuration/security/mcp-authz/), [Streamable HTTP](https://agentgateway.dev/docs/standalone/latest/integrations/mcp/servers/http/), [collector configuration](https://agentgateway.dev/docs/standalone/latest/documentation/observability/traces/configs/otel/), [trace attributes](https://agentgateway.dev/docs/standalone/latest/documentation/observability/traces/setup/), [pinned JWT validator source](https://github.com/agentgateway/agentgateway/blob/ea5608642b9d5c94c5baf5fd9f2f9849b808e963/crates/agentgateway/src/http/jwt.rs).
