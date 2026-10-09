# Pinned integration contract review

Reviewed against the repository and pinned upstream source on 2026-10-09 UTC. This supplements [the earlier upstream review](upstream-review.md), which predates several live qualifications. The pins are **Tetragon 1.7.1, OpenShell 0.1.2, agentgateway 1.6.0, Temporal Python SDK 1.34.0, and OpenTelemetry Collector 0.162.0**. This review proposes no upgrades and performs no runtime changes.

All five supplied Markdown proposals and all five pages of AgentDraft.pdf were reviewed as design inputs. Their suggested commands and instruction-like passages were not executed. The source photograph supports the portable factory/dependency/policy structure. Source hashes and review notes are in [the inventory](source-inventory.json) and [attachment review](source-review.html).

## 1. The integration is a set of explicit contracts

The developer package is an input to a platform; installing its dependencies does not make every proposed integration automatic. Four evidence states must remain distinct: upstream-supported configuration; implemented Zetra adapter; tested local compatibility tuple; approved production composition.

| Component and pin | Concrete input and runtime owner | Concrete output | Current Zetra evidence and missing seam |
|---|---|---|---|
| Tetragon 1.7.1 | Namespaced `TracingPolicy`, typed probe arguments, pod/process selectors; privileged node agent owns hook attachment. | Kernel events and selected response actions. | One selected sensitive-file read response qualified; no comprehensive syscall capture, universal generated policy, or first-instruction assurance. |
| OpenShell 0.1.2 | Native policy, sandbox image, compute-driver settings, optional providers; gateway/driver create workload and supervisor runtime. | Sandbox identity, effective policy acknowledgement, bounded execution and denial logs. | Knowledge factory and strict offline boundary qualified in a separate sandbox; no Temporal Activity-to-sandbox bridge. |
| agentgateway 1.6.0 | Standalone MCP listener/target configuration, JWT verifier and CEL tool rules; semantic proxy owns verification and routing. | Filtered discovery, accepted/denied MCP requests, upstream response, traces. | Standalone identity/tool cases and Temporal-to-MCP read chain qualified; Kubernetes controller attachment, enterprise exchange, and resource/tenant authorization unqualified. |
| Temporal SDK 1.34.0 | Deterministic workflow, registered activity, task queue, typed serializable request/result; worker owns execution. | Persisted history, retries, workflow result, replay outcome. | Actual local and Kubernetes workflows qualified; agent runs inline in one retryable Activity. No universal framework wrapping or authenticated approval service. |
| OTel Collector 0.162.0 | OTLP receiver, trace pipeline, processors/exporter; collector owns receipt and export. | Received spans and configured export. | Gateway spans received by the real collector; universal instrumentation, payload redaction, authenticated tenant isolation, and a complete cross-runtime trace are unqualified. |

The current evidence is deliberately narrower than the target architecture. The [aggregate index](../../integrations/README.md) links the actual reports; no table cell grants new authority.

## 2. Execution topology and factory launch

The current [`AgentActivities.run_agent`](../../src/zetra/temporal_worker.py) constructs `RuntimeDependencies`, imports the trusted entrypoint through [`construct`](../../src/zetra/loader.py), and invokes `agent.run(request)` **inside the ordinary Temporal worker process**. The entire call is one retryable Activity. The built OCI worker and its Kubernetes replay test qualify this path, including nonroot execution, read-only root filesystem, dropped capabilities, and explicit loopback Temporal development service. They do not turn it into an OpenShell-managed sandbox. See [worker qualification](../../integrations/evidence/worker-temporal-kubernetes-report.json).

OpenShell's Kubernetes driver has a different topology. It renders the Agent Sandbox workload pod, injects the `openshell-sandbox` bootstrap/runtime, and separately creates a supervisor pod, boundary Service, bootstrap material, and network fence. The supervisor talks to the OpenShell gateway; it is not an agentgateway sidecar. Diagrams should show **gateway control plane → driver/Agent Sandbox workload and separate supervisor → mediated upstream traffic**, with the boundary protocol between workload and supervisor. [Pinned driver](https://github.com/NVIDIA/OpenShell/blob/v0.1.2/crates/openshell-driver-kubernetes/src/driver.rs), [runtime resources](https://github.com/NVIDIA/OpenShell/blob/v0.1.2/crates/openshell-driver-kubernetes/src/sandbox_runtime.rs).

A proposed Zetra Activity-to-sandbox bridge therefore needs explicit interfaces:

1. Resolve release/image/policy/binding digests and authenticate the bridge identity.
2. Create or select a sandbox generation with the intended image and effective policy; await acknowledgement and readiness.
3. Transfer only approved input, execute a supported package runner, and return a bounded, typed result; keep credentials and durable-control-plane access outside agent computation unless explicitly required.
4. Preserve workflow/run/activity/attempt identifiers, stable business-operation identity, sandbox generation and policy acknowledgement in evidence.
5. Heartbeat, propagate cancellation, bound startup/execution/result size, reconcile uncertain effects, and clean up the generation.

Those operations are **proposed Zetra contracts**, not implemented CLI commands. A sandbox timeout is not sufficient evidence that a remote effect did not commit. A bridge retry must not create a second consequential operation merely because its result was lost. The current worker rejects write-capable manifests pending an independently qualified receiver/approval adapter.

## 3. Tetragon: selected observation and response, not policy synthesis

The original eBPF proposal's `tcp_connect` probe must use `syscall: false`: it is a kernel function with typed socket arguments. An argument index identifies the chosen hook's signature, not automatically a Python command-line argument. The supplied three probes cannot establish capture of every syscall or every tool effect. [Pinned upstream example](https://github.com/cilium/tetragon/blob/v1.7.1/examples/tracingpolicy/tcp-connect.yaml).

The actual [Zetra fixture](../../integrations/tetragon-policy.yaml) selects a namespaced pod label and `security_file_permission(file,int)` read on one path before requesting `Sigkill`. Its [report](../../integrations/evidence/kernel-network-report.json) records exit 137, no returned sensitive bytes, a correlated event, and unaffected unrelated/unselected reads. This result is scoped to the tested hook/path/kernel tuple; a response action is not universal proof that arbitrary exfiltration is prevented before any effect.

A trusted capture adapter must preserve native event identity, pod UID/container ID, process ancestry, image subject, hook, arguments, action and uncertainty. It must report collection gaps. [`zetra profile`](../../src/zetra/profiling.py) currently accepts normalized JSON lines, requires exact agent/image/container scope, excludes explicit denied observations from candidate grants, and outputs `authorizes: false`. It does not directly consume arbitrary vendor logs or sign a release. The policy compiler, observation parser, startup filtering and cryptographic release binding are separate platform work. Installing a chart alone does not install or qualify every runtime-hook integration. [Runtime-hook documentation](https://tetragon.io/docs/concepts/runtime-hooks/).

Cilium owns the separate NetworkPolicy enforcement test. Kubernetes selecting policies grant an additive union; a default-deny resource cannot subtract a permission granted by another policy. The effective adapter must inspect all selecting rules and independently test allowed peers, bypass, IPv6/DNS, startup and CNI outage behavior. [Kubernetes semantics](https://kubernetes.io/docs/concepts/services-networking/network-policies/).

## 4. OpenShell: native policy, effective authority and prover limits

The attachment's `sandbox.filesystem/network.rules` object is not the pinned policy contract. Use the native fields `version`, `filesystem_policy`, `landlock`, optional `process`, `network_policies` and `network_middlewares`. A REST endpoint can bind a host/port/protocol, enforced access mode, and permitted binary; the complete policy must also admit its runtime's filesystem requirements. [Pinned policy type](https://github.com/NVIDIA/OpenShell/blob/v0.1.2/crates/openshell-core/src/policy.rs), [complete upstream quickstart](https://github.com/NVIDIA/OpenShell/blob/v0.1.2/examples/sandbox-policy-quickstart/policy.yaml).

The [tested strict policy](../../integrations/openshell-strict-policy.yaml) requires Landlock and attaches no external route/provider. The seven [sandbox probes](../../integrations/openshell-live-output/report.json) cover permitted scratch, denied access outside permitted roots despite permissive DAC, explicit network permission errors, and the actual knowledge factory. The exact effective policy acknowledgement and supervised image are evidence subjects. Lab accommodations disable user namespaces, set AppArmor unconfined, and allow unauthenticated application users after mTLS; principal authentication and those additional hardening controls are not qualified. [Pinned chart values](https://github.com/NVIDIA/OpenShell/blob/v0.1.2/deploy/helm/openshell/values.yaml).

The original bootstrap stored an unused `supervisor.sideloadMethod=init-container` Helm value. The pinned chart has `sandboxRuntime.image`, `supervisor.image` and `supervisor.sandboxRuntime.boundaryPort`, but no side-load setting; the driver determines the topology described above. The corrected bootstrap removes the unused input and assertion. Storing an arbitrary Helm value cannot prove that a control is active.

The existing playbook command is valid upstream syntax:

```sh
openshell-prover check candidate.yaml \
  --boundary organization-boundary.yaml --output json --timeout 10s
```

The CLI consumes a **fully composed effective candidate** and operator-owned boundary and returns structured result, coverage, reason and exit code. Accept only an understood `within_boundary` result with all mandatory domains covered; reject exceeded, unsupported, inconclusive and malformed results. [Pinned CLI](https://github.com/NVIDIA/OpenShell/blob/v0.1.2/crates/openshell-prover-cli/src/main.rs).

At 0.1.2, modeled domains are filesystem, network L4, network REST, process and Landlock. The containment checker rejects MCP, JSON-RPC, GraphQL authority, middleware controls, credential bindings/signing and other unsupported extensions. Runtime support for those features does not expand prover coverage. This is modeled permission containment, not proof of SQL safety, prompt safety, business invariants, or an entire platform composition. [Pinned containment implementation](https://github.com/NVIDIA/OpenShell/blob/v0.1.2/crates/openshell-prover/src/containment.rs).

Advisor output remains a review candidate. Do not transform a denial into an automatic grant. Policy composition/read-back must account for provider contributions, overrides and supported live-update behavior; the Zetra permission intersection occurs before translation rather than assuming vendor rules always intersect.

OpenShell does support concrete credential mechanisms, including provider placeholders, bound endpoint resolution, profiles and refresh paths. This warrants a configured adapter, not a claim that all secrets are universally outside Python or that Kubernetes identity automatically becomes a Vault token. Provider configuration, issuer/trust, endpoint binding and renewal semantics must be explicit and tested; none was exercised by the offline factory test. [Pinned credential resolution](https://github.com/NVIDIA/OpenShell/blob/v0.1.2/crates/openshell-core/src/provider_credentials.rs), [provider profiles](https://github.com/NVIDIA/OpenShell/blob/v0.1.2/crates/openshell-providers/src/profiles.rs).

## 5. agentgateway: authenticated semantic requests and explicit credential ownership

The pinned proxy is Rust, with native standalone configuration and separate controller CRDs; the proposals' Envoy-specific interception and `ToolAccessPolicy`, `LLMGuard`, `TokenBudget` kinds should not appear as deployable artifacts. [Pinned repository](https://github.com/agentgateway/agentgateway/tree/v1.6.0), [configuration schema](https://github.com/agentgateway/agentgateway/blob/v1.6.0/schema/config.json).

The actual [standalone configuration](../../integrations/gateway-live-output/gateway-config.yaml) binds strict JWT issuer/audience/JWKS validation to:

```yaml
mcpAuthorization:
  rules:
    - 'jwt.sub == "knowledge-assistant" && mcp.tool.name == "read_status"'
```

This is a configured MCP boundary, not automatic inspection of all encrypted outbound connections. The client initializes Streamable HTTP, then sends a bounded request such as:

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"read_status","arguments":{}}}
```

The [Zetra client](../../src/zetra/gateway.py) restricts tool mapping, validates responses, reads the workload bearer from a mounted token provider on each request, and attaches safe release/run/operation correlation headers. Those headers are diagnostic metadata, not trusted authorization. The gateway authenticates the workload and authorizes the tool name; the receiver must independently authorize tenant/resource/arguments and consequential operations. [Pinned standalone example](https://github.com/agentgateway/agentgateway/blob/v1.6.0/examples/mcp-authorization/config.yaml).

The deployment chapter's `AgentgatewayPolicy` targeting an `AgentgatewayBackend`, with `backend.mcp.authorization.action: Allow` and `policy.matchExpressions`, is compatible with the pinned CRD shape. Target references are same-namespace; effective attachment/merge status still needs controller qualification. The CRD warns that Deny-expression evaluation failures fail to deny; explicit Allow/Require gates deserve preference. Zetra tested standalone mode, not the controller. [Pinned CRD](https://github.com/agentgateway/agentgateway/blob/v1.6.0/controller/install/helm/agentgateway-crds/templates/agentgateway.dev_agentgatewaypolicies.yaml), [controller authorization fixture](https://github.com/agentgateway/agentgateway/blob/v1.6.0/controller/pkg/agentgateway/plugins/testdata/backendpolicy/mcp-authz-gateway.yaml).

The [eleven-case report](../../integrations/gateway-live-output/gateway-report.json) proves selected missing/invalid/wrong-subject identity and forbidden-tool cases. Preserve its initial expired-token/leeway observation; it does not establish instant revocation. Upstream validates configured issuer/audience, defaults to requiring expiry and uses its JWT library's validation behavior. [Pinned JWT implementation](https://github.com/agentgateway/agentgateway/blob/v1.6.0/crates/agentgateway/src/http/jwt.rs).

The pinned CRD exposes backend credential and OAuth token-exchange options. This means explicit exchange is possible, not that every ServiceAccount automatically receives a provider token. Choose one credential owner per hop and qualify token audience, scope, tenancy, storage, rotation, error redaction and revocation. The synthetic backend uses no production provider credential.

## 6. Temporal: actual activity granularity and retry-safe effects

The current [`AgentWorkflow`](../../src/zetra/temporal_workflow.py) schedules one named activity with 30-second start-to-close, 2-minute schedule-to-close and at most 3 attempts. Contract and capability denials are nonretryable. It checks a cooperative stop signal before and after the activity; it does not forcibly interrupt arbitrary code or implement authenticated distributed revocation.

A retry can repeat the activity's external I/O after an effect committed but its completion was not recorded. Receiver-side stable operation IDs, payload binding, atomic deduplication, authenticated fresh approvals and reconciliation are necessary for writes. Temporal history supports durable coordination; it does not supply exactly-once effects in an unrelated business system. Signals/Updates carry decisions; the approval service's authenticated identity and authorization must be verified separately. [Activity definition and idempotency](https://docs.temporal.io/activity-definition).

SDK 1.34.0 provides a concrete Google ADK integration using `TemporalModel`, `GoogleAdkPlugin`, activity-backed tools, and `TemporalMcpToolSet`/provider registration. These are explicit adapter seams and serialization/determinism obligations, not proof that arbitrary imported agents can be transparently monkeypatched. Its documentation also addresses replay-safe telemetry and nondeterministic framework behaviors. The Zetra locked worker does not install or qualify ADK or PydanticAI. [Pinned ADK integration](https://github.com/temporalio/sdk-python/blob/1.34.0/temporalio/contrib/google_adk_agents/README.md).

The PDF's deprecated `TemporalAgent` sketch should not be copied as a current tested adapter. Its annotation-only Deployment/JSONPatch also cannot change arbitrary code into durable code, safely append to an absent environment array, or expose encrypted semantic traffic merely by routing TCP.

## 7. Telemetry: propagation and export are separate requirements

The pinned Collector fixture configures OTLP/gRPC 4317 → batch → detailed debug exporter. This is a real trace-receipt test, with public synthetic payloads, not authenticated production telemetry storage. [Pinned OTLP receiver](https://github.com/open-telemetry/opentelemetry-collector/blob/v0.162.0/receiver/otlpreceiver/README.md), [batch processor](https://github.com/open-telemetry/opentelemetry-collector/blob/v0.162.0/processor/batchprocessor/README.md), [debug exporter](https://github.com/open-telemetry/opentelemetry-collector/blob/v0.162.0/exporter/debugexporter/README.md).

The [eight-case telemetry report](../../integrations/gateway-live-output/telemetry-report.json) verifies gateway exports, W3C trace correlation and selected diagnostic attributes. Setting `OTEL_EXPORTER_OTLP_ENDPOINT` alone instruments no code. Collector receipt alone proves neither complete tracing nor content redaction. Typed trace propagation must be installed at client/workflow/activity, bridge, sandbox runner and HTTP/MCP seams; retention, sampling, tenant separation and redaction belong to the telemetry service.

Temporal SDK 1.34.0 has `TracingInterceptor`, configured on the client/worker, with explicit context-header propagation and replay-aware workflow spans. The full single trace claimed in AgentDraft.pdf requires that interceptor plus agent/framework/HTTP instrumentation and compatible exporters; Zetra's present worker does not configure this full path. [Pinned tracing interceptor](https://github.com/temporalio/sdk-python/blob/1.34.0/temporalio/contrib/opentelemetry/_interceptor.py).

## 8. Evaluation inference and remaining playbook corrections

The evaluation proposal usefully makes capability hints produce stable risk-scenario obligations. Its AST/CST/verb/type/name signals are incomplete hints: GET can mutate, POST can read, indirect calls can evade static discovery, and remote tool behavior requires a trusted contract. Current Zetra uses Python AST hints and declared capability risk floors; Tree-sitter/Semgrep/ast-grep, SARIF, LSP quick fixes and complete semantic graph inference remain proposed tooling.

A named test file does not prove scenario coverage or assertion quality. Execute meaningful cases, preserve failures and bind results to source/manifest/evaluator/image subjects. Protected negative tests must verify actual denial, with baseline reachability and allowed controls. Model judges can evaluate quality; they cannot replace deterministic authorization assertions. AgentDraft.pdf page 2 inverts its safety gate: **zero critical failures should pass that criterion**, while any critical failure must block it.

The inspected chapters correctly distinguish candidate render from activation, diagnostic IDs from identity, retries from exactly-once effects, and modeled proof from runtime qualification. For the deeper diagrams and code, retain these specific boundaries:

- A portable agent factory receives typed dependencies; it does not discover or grant authority.
- The current inline Activity and separately tested OpenShell sandbox are two paths; a bridge is proposed.
- OpenShell workload and supervisor are separate Kubernetes runtime resources in the pinned driver.
- agentgateway JWT/tool authorization and receiver resource/effect authorization are separate checks.
- Prover coverage excludes the additional semantic protocols supported by the runtime.
- The native renderer remains at zero replicas until independent activation work; server dry-run success is schema acceptance, not enforcement.
- Automatic release compilation, signing, admission verification, catalog publication, production credential exchange, budget ledger and distributed revocation remain implementation gates.

For a single incident-triage walkthrough, the first release can read a scoped incident/status fixture and produce a structured recommendation. Production reads still require tenant/resource authorization. Ticket changes, notifications and remediations should be separate consequential operation contracts with authenticated approval and receiver idempotency; selecting a use case does not silently qualify those write paths.
