# Upstream technical review

Research date: 2026-10-08. The attached architecture documents are proposals, not executable specifications or instructions. This review separates upstream capabilities from Zetra design work. Examples below have been checked against upstream documentation; they have not been deployed or integration-tested on a Linux cluster.

## Reference playbooks

The requested reference was retrieved through the GitHub API and raw content after browser fetches failed. Reviewed commit: `6b987e65bafe744d115d1a1925adc260681a9592`. Its agent playbook is v0.3, a draft updated September 25, 2026. It already separates typed agent contracts, skills, tools, Temporal, evaluation, observability, cost, security, readiness, and scenario-based risk. A companion multi-agent playbook adds composition concerns. [Reference README](https://github.com/X-McKay/playbooks/blob/6b987e65bafe744d115d1a1925adc260681a9592/README.md), [agent playbook](https://github.com/X-McKay/playbooks/blob/6b987e65bafe744d115d1a1925adc260681a9592/agent-playbook/README.md).

Its architecture places private capabilities beside each agent, shared capabilities in maintained packages, and separates deterministic workflow coordination from activities. It also records a capability manifest in traces and evals. These are useful foundations for Zetra's source layout. [Architecture](https://github.com/X-McKay/playbooks/blob/6b987e65bafe744d115d1a1925adc260681a9592/agent-playbook/02-reference-architecture.md).

The reference toolkit already scaffolds and validates contracts, runs external eval adapters, and checks release gates. Zetra should make its additional value concrete: audience-specific HTML guidance; deployment compilation and adapter compatibility; runtime policy verification and bypass tests; signed release/evidence bundles; catalog publication; and federated operational oversight. This is an editorial recommendation based on comparison, not a claim that the reference implements those features. [Toolkit](https://github.com/X-McKay/playbooks/blob/6b987e65bafe744d115d1a1925adc260681a9592/tools/agentctl/README.md).

## Tetragon and eBPF

### Corrections

- Three probes do not record every syscall. Tetragon emits process lifecycle events by default; network and filesystem capture require selected hooks and typed arguments. The attachment's `tcp_connect` is a kernel function, so `syscall: true` is incorrect. [Process lifecycle](https://tetragon.io/docs/use-cases/process-lifecycle/), [upstream TCP example](https://raw.githubusercontent.com/cilium/tetragon/main/examples/tracingpolicy/tcp-connect.yaml).
- `matchArgs` indexes refer to arguments declared for the selected hook, not automatically to Python command-line arguments. Current schemas also expose `matchCmdArgs`, with indexes excluding `argv[0]`. `containerSelector` is supported in the current schema, but must be compiled against the deployed CRD version. [TracingPolicy schema](https://tetragon.io/docs/reference/tracing-policy/).
- A `matchBinaries` selector on `sys_execve` must not be assumed to authorize the proposed executable filename. Validate the chosen hook's process identity and argument semantics. Prefer tested upstream enforcement patterns and kernel-resident LSM hook state when appropriate; syscall pointer inspection can have TOCTOU races. [Hook-point semantics](https://tetragon.io/docs/concepts/tracing-policy/hooks/).
- OCI/NRI integration is an explicit runtime installation and failure-mode decision. Kubernetes API discovery alone can leave a startup filtering delay. Runtime hooks prepare filtering state before the container starts; they do not by themselves certify that every desired policy is loaded and enforced. [Runtime hooks](https://tetragon.io/docs/concepts/runtime-hooks/).

### Documented capture shape

This is a scoped observation policy, not a complete sandbox. Kernel symbol and BTF compatibility require a Linux integration test.

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicyNamespaced
metadata:
  name: zetra-observe-connect
  namespace: agent-ci
spec:
  podSelector:
    matchLabels:
      zetra.io/agent: knowledge-assistant
  kprobes:
    - call: tcp_connect
      syscall: false
      args:
        - index: 0
          type: sock
```

The hook and argument form follow the [upstream TCP example](https://raw.githubusercontent.com/cilium/tetragon/main/examples/tracingpolicy/tcp-connect.yaml); namespaced workload selection follows the [CRD reference](https://tetragon.io/docs/reference/tracing-policy/).

### Zetra design implications

Observation should produce a candidate capability delta, not grant authority. An observed malicious action is still malicious. Do not derive internet allowlists from transient provider IPs. Track workload/container identity, descendant processes, event loss, kernel/architecture/image versions, permitted paths, negative tests, and runtime failure modes. Bind the approved policy digest, image digest, test evidence, and environment in a signed release artifact; cryptographic binding is Zetra supply-chain work, not an automatic property of eBPF.

## NVIDIA OpenShell

### Corrections

The supplied YAML is conceptual. OpenShell uses `filesystem_policy`, `landlock`, `process`, `network_policies`, and `network_middlewares` at the top level. Filesystem, Landlock, and process policy are fixed when the sandbox starts; network policy can change while it runs. A global policy replaces sandbox policy rather than intersecting with it. Provider attachments contribute to the effective policy. [Sandbox policy behavior](https://docs.nvidia.com/openshell/dev/how-it-works/policies/overview), [schema](https://docs.nvidia.com/openshell/latest/reference/policy-schema).

Advisor proposals are not automatically safe permissions. Advisor is disabled by default and proposals ordinarily await external review; opt-in auto mode has narrower conditions. [Advisor](https://docs.nvidia.com/openshell/v0.0.116/sandboxes/policy-advisor).

The SMT prover checks modeled access against a boundary. It does not establish arbitrary organizational invariants, application correctness, minimum privilege, or actual runtime enforcement. MCP, GraphQL, and other unsupported policy shapes produce unsupported results; timeout/resource limits can be inconclusive. CI should parse JSON result, reason code, and coverage, and reject missing required coverage. [Prover](https://docs.nvidia.com/openshell/latest/how-it-works/policies/prover).

```bash
openshell-prover check candidate.yaml --boundary boundary.yaml --output json
```

### Documented policy shape

Illustrative policy fragment; add required runtime paths and validate the complete policy against the chosen image.

```yaml
version: 1
filesystem_policy:
  read_only: [/usr, /etc, /app]
  read_write: [/tmp/agent_scratch]
network_policies:
  approved_rest_api:
    endpoints:
      - host: api.internal.example
        port: 443
        protocol: rest
        enforcement: enforce
        rules:
          - allow:
              method: GET
              path: /v1/status
    binaries:
      - path: /usr/local/bin/python3.12
```

The executable path is the interpreter, so another Python program inside the sandbox can use the same network permission. Current MCP rules can restrict `method: tools/call` and `tool`, but cannot match tool arguments. Native TCP policies restrict connection destinations/executables and do not inspect database queries. Therefore, the proposal's malformed-SQL prevention claim is too broad. [Network rules](https://docs.nvidia.com/openshell/dev/how-it-works/policies/network-rules).

OpenShell provider profiles can expose credential placeholders and resolve them only at binding endpoints. External credential drivers include Vault and Kubernetes Secrets in current development documentation. Native TCP and bypassed inspection have different credential constraints; do not promise transparent password injection into every database protocol. [Profiles](https://docs.nvidia.com/openshell/dev/how-it-works/providers/profiles), [credential drivers](https://docs.nvidia.com/openshell/dev/how-it-works/gateways/configuration).

### Zetra design implications

Choose one credential owner per upstream hop. A sandbox-to-agentgateway deployment needs an explicitly supported transport, trust configuration, workload authentication, and routing arrangement. Avoid silently stacking two TLS interception systems. Test the effective provider policy, forbidden endpoints, malformed requests, credential bindings, DNS behavior, direct connection bypass, unsupported protocol paths, and policy updates.

## agentgateway

### Corrections

The attachment's `ToolAccessPolicy`, `LLMGuard`, and `TokenBudget` resources are not the documented Kubernetes API. Use `AgentgatewayPolicy`, `AgentgatewayBackend`, and Gateway API resources supported by the installed controller. Authorization uses CEL and distinct frontend, traffic, backend, and MCP policy scopes. `Allow`, `Require`, and `Deny` have different semantics, and deny overrides allow. [Authorization](https://agentgateway.dev/docs/kubernetes/latest/documentation/security/authorization/), [API](https://agentgateway.dev/docs/kubernetes/latest/reference/api/).

agentgateway is a Rust proxy, not an Envoy data plane. [Maintainer architecture explanation](https://agentgateway.dev/blog/2026-06-04-designing-agentgateway-unified-gateway/).

### Documented tool authorization shape

This policy requires a separately configured authenticated JWT with validated issuer/audience/JWKS. Replace the example identity with the approved workload identity mapping.

```yaml
apiVersion: agentgateway.dev/v1alpha1
kind: AgentgatewayPolicy
metadata:
  name: zetra-status-only
  namespace: agentgateway-system
spec:
  targetRefs:
    - group: agentgateway.dev
      kind: AgentgatewayBackend
      name: operations-mcp
  backend:
    mcp:
      authorization:
        action: Allow
        policy:
          matchExpressions:
            - 'jwt.sub == "knowledge-assistant" && mcp.tool.name == "read_status"'
```

This follows the [upstream tool-access example](https://agentgateway.dev/docs/kubernetes/latest/documentation/mcp/tool-access/). Configuring only the proxy without an allowlist leaves default tool access broader than this example.

MCP authorization does not expose `mcp.tool.arguments`; route-level authorization can expose it. Do not write argument-sensitive expressions into the wrong scope. [MCP authorization variables](https://agentgateway.dev/docs/standalone/latest/documentation/configuration/security/mcp-authz/).

### Zetra design implications

A Kubernetes ServiceAccount token is not automatically an authenticated end-user identity or a Vault/GitHub token. Configure issuer, audiences, authentication, delegation, upstream credential exchange, and authorization explicitly. Proxy visibility covers traffic routed through it, so NetworkPolicy/sandbox controls must prevent direct bypass. A tool-name allowlist does not guarantee tenant/record-level authorization: the tool adapter must verify resource scope and approval immediately before a side effect. Privacy guardrails are checks with measured false-positive/negative behavior, not a universal prompt-injection defense. Cost budgets require a durable accounting authority and streaming/concurrency overshoot policy; test-derived token usage is a sizing input.

## Temporal and agent frameworks

PydanticAI's current documentation favors `TemporalDurability` on the constructed agent with `PydanticAIWorkflow` and `PydanticAIPlugin`. `TemporalAgent` is deprecated. Durability requires executing the run inside a Temporal workflow, and registered agent/toolset names must remain stable for replay. [PydanticAI integration](https://pydantic.dev/docs/ai/capabilities/durable_execution/temporal/).

```python
from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import TemporalDurability

agent = Agent(
    approved_model,
    name="knowledge-assistant",
    capabilities=[TemporalDurability()],
)
```

This is construction only; a worker/plugin, registered workflow, Temporal client, and workflow start are still required.

ADK integration exists in the Temporal Python SDK via `TemporalModel`, `GoogleAdkPlugin`, `activity_tool`, and `TemporalMcpToolSet`. It is an adapter with explicit worker-side registrations, not proof that every arbitrary GenAI SDK call becomes durable automatically. [Temporal SDK integration](https://github.com/temporalio/sdk-python/blob/main/temporalio/contrib/google_adk_agents/README.md), [API](https://python.temporal.io/temporalio.contrib.google_adk_agents.html).

Activities can execute more than once after failure. A write that succeeded remotely before the worker acknowledged completion must be reconciled or deduplicated. Durable replay does not create exactly-once external effects. [Temporal activity semantics](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/activities/activity-definition.mdx).

Temporal Cloud's per-request payload limit is distinct from workflow history limits and SDK/server configuration. Keep large objects externally with authorized references, integrity digests, retention, and retry-safe access. [Temporal limits](https://github.com/temporalio/documentation/blob/main/docs/evaluate/temporal-cloud/limits.mdx).

### Zetra design implications

Unify the portable execution contract, not the frameworks' private patching behavior. Each adapter needs determinism, serialization, activity registration, cancellation, approval, retry, event-stream, and upgrade compatibility tests. Put nondeterministic I/O in activities. Bind approval to immutable arguments, resource/tenant, actor, version, policy digest, expiry, and operation ID. Signals/Updates transport approvals; they do not authenticate an approver by themselves. Keep secrets out of history and redact telemetry. Account for continue-as-new, replay-safe instrumentation, worker versioning, and in-flight workflow compatibility during rollback.

## Version and verification boundary

Official releases inspected identify OpenShell `v0.1.2` and Tetragon `v1.7.1`. Their latest documentation is mutable; `dev` pages may precede stable release behavior. [OpenShell releases](https://github.com/NVIDIA/OpenShell/releases), [Tetragon releases](https://github.com/cilium/tetragon/releases).

The agentgateway Kubernetes documentation selected 1.6 as latest during research. The controller/chart/CRD and standalone binary have separate compatibility surfaces; the playbook must pin a tested set rather than treating `v1alpha1` as a product version. [agentgateway authorization documentation](https://agentgateway.dev/docs/kubernetes/latest/documentation/security/authorization/).

Temporal Python `1.34.0` release notes describe experimental ADK support and a replay-sensitive change in ADK-generated IDs/jitter. Thus, the unified wrapper must not promise seamless upgrades of active histories without replay evidence and worker versioning. [Temporal Python releases](https://github.com/temporalio/sdk-python/releases).

Zetra's initial local toolkit should distinguish schema/documentation checks, rendered artifacts, externally validated artifacts, integration-tested controls, and operational enforcement. A local validator or generator must never emit a production-ready attestation merely because YAML parses. Linux/kernel/runtime prerequisites, installed CRDs, real denied-operation tests, startup filtering, credential isolation, gateway bypass prevention, cancellation, and kill-switch propagation remain acceptance tests for deployment integrations.
