"""Section 3 of the specification: names every component uses verbatim.

Fixed values are module constants. Deployment inputs (the `<...>` values) live in the
frozen `PlatformConfig`, read once from the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# 3.1 Namespaces and workloads
PLATFORM_NAMESPACE = "agent-platform"
GATEWAY_NAMESPACE = "ai-gateway"
IDP_NAMESPACE = "auth"
MODEL_NAMESPACE = "llm"
MCP_NAMESPACE = "mcp"
TENANT_PATTERN = r"^tenant-[a-z0-9-]{1,50}$"
GATEWAY_NAME = "agents"
GATEWAY_CLASS = "agentgateway"
GATEWAY_POD_LABEL = {"app.kubernetes.io/name": "agents"}
KATA_RUNTIME_CLASS = "kata-qemu-runtime-rs"

# 3.2 Network identifiers
GATEWAY_PORT = 443
GATEWAY_CA_CONFIGMAP = "agent-gateway-ca"
GATEWAY_CA_KEY = "ca.crt"
GATEWAY_CA_MOUNT = "/etc/agent-platform"
GATEWAY_CA_PATH = "/etc/agent-platform/ca.crt"
TOKEN_HOST = "token.agents.internal"
INFERENCE_HOST = "inference.agents.internal"
MCP_HOST = "mcp.agents.internal"
DEFAULT_TRUST_DOMAIN = "cluster.local"

# 3.3 Identity provider
TOKEN_ROUTE_PATH = "/v1/token"
CLUSTER_ISSUER = "https://kubernetes.default.svc.cluster.local"
CLUSTER_JWKS = "https://kubernetes.default.svc.cluster.local/openid/v1/jwks"
AGENT_TOKEN_AUDIENCE = "agent-gateway"
AGENT_GATEWAY_SCOPE = "agent-gateway"
APS_AUDIENCE = "agent-policy-service"
CLIENT_TYPE_AGENT = "agent"

# 3.4 Tokens and lifetimes
PROJECTED_TOKEN_SECONDS = 600
AGENT_TOKEN_SECONDS = 300

# 3.6 Labels, names and identifiers
API_GROUP = "agentplatform.io"
API_VERSION = "v1alpha1"
GROUP_VERSION = f"{API_GROUP}/{API_VERSION}"
LABEL_PREFIX = "agentplatform.io/"
LABEL_SANDBOX = LABEL_PREFIX + "sandbox"
LABEL_AGENT = LABEL_PREFIX + "agent"
LABEL_TENANT = LABEL_PREFIX + "tenant"
LABEL_SESSION = LABEL_PREFIX + "session"
LABEL_GENERATION = LABEL_PREFIX + "generation"
LABEL_RELEASE = LABEL_PREFIX + "release"
ANNOTATION_POLICY_DIGEST = LABEL_PREFIX + "policy-digest"
ANNOTATION_NETPOL_DIGEST = LABEL_PREFIX + "netpol-digest"
ANNOTATION_NETPOL_ATTESTED = LABEL_PREFIX + "netpol-attested"
POLICY_BUNDLE_KEY = "bundle.json"
POD_TEMPLATE_KEY = "pod.yaml"
AGENT_SUBJECTS_CONFIGMAP = "agent-subjects"
RATELIMIT_CONFIGMAP = "ratelimit-config"
RATELIMIT_KEY = "agents.yaml"
RATELIMIT_DOMAIN = "agents"

# 3.7 Timeouts and limits
TOKENS_PER_DAY = 2_000_000
TOKENS_PER_SESSION = 200_000
SANDBOX_TTL_SECONDS = 1800
MAX_BOUNDARIES = 64
ATTESTATION_REFRESH_SECONDS = 30
ATTESTATION_MAX_AGE_SECONDS = 90

# 5.2 Sandbox pod
SANDBOX_UID = 1500
EGRESS_GUARD_TIMEOUT_SECONDS = 90
AGENT_COMMAND = ["/app/.venv/bin/python", "-m", "agent_runtime.wait"]
AGENT_TOKEN_PATH = "/var/run/secrets/agent-identity/token"

# 5.3 Tenant NetworkPolicies
TENANT_NETWORK_POLICIES = (
    "default-deny-all",
    "sandbox-egress-gateway-only",
    "sandbox-ingress-none",
    "tenant-no-other-egress",
)

# Platform principals (9.1, 10.6, 10.7, 5.5)
APS_SERVICE_ACCOUNT = "agent-policy-service"
RUNNER_SERVICE_ACCOUNT = "session-runner"
RECONCILER_SERVICE_ACCOUNT = "cleanup-reconciler"
ATTESTOR_SERVICE_ACCOUNT = "netpol-attestor"


@dataclass(frozen=True)
class PlatformConfig:
    """Deployment inputs of section 3 (values written as `<...>` in the spec)."""

    issuer_url: str  # https://idp.<domain>/realms/agents, exactly as the IdP emits it
    mcp_resource_base: str  # https://mcp.<domain>; resource URL is <base>/<backend>/mcp
    gateway_cluster_ip: str  # fixed spec.clusterIP of Service agents
    egress_guard_image: str  # registry/.../egress-guard@sha256:<digest>
    trust_domain: str = DEFAULT_TRUST_DOMAIN
    forbidden: tuple[str, ...] = ("10.43.0.1:443", "10.43.0.10:53")  # API server, kube-dns
    ca_configmap: str = GATEWAY_CA_CONFIGMAP

    def __post_init__(self) -> None:
        if "@sha256:" not in self.egress_guard_image:
            raise ValueError("egress guard image must be pinned by digest")
        if self.mcp_resource_base.endswith("/"):
            raise ValueError("mcp_resource_base must not end with '/'")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> PlatformConfig:
        e = os.environ if env is None else env
        forbidden = e.get("AGENT_PLATFORM_FORBIDDEN")
        return cls(
            issuer_url=e["AGENT_PLATFORM_ISSUER_URL"],
            mcp_resource_base=e["AGENT_PLATFORM_MCP_RESOURCE_BASE"],
            gateway_cluster_ip=e["AGENT_PLATFORM_GATEWAY_CLUSTER_IP"],
            egress_guard_image=e["AGENT_PLATFORM_EGRESS_GUARD_IMAGE"],
            trust_domain=e.get("AGENT_PLATFORM_TRUST_DOMAIN", DEFAULT_TRUST_DOMAIN),
            forbidden=tuple(forbidden.split(",")) if forbidden else cls.forbidden,
            ca_configmap=e.get("AGENT_PLATFORM_CA_CONFIGMAP", GATEWAY_CA_CONFIGMAP),
        )
