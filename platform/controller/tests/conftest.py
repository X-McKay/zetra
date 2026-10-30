from __future__ import annotations

import copy
import re

import pytest

from agent_platform.bundle import compile_bundle, digest_hex
from agent_platform.constants import PlatformConfig
from registry_controller.config import ControllerConfig
from registry_controller.reconciler import Reconciler
from tests.fakes import FakeIdp, FakeKube, FakeVerifier

JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
BEARER_RE = re.compile(r"Bearer [A-Za-z0-9._~+/=-]{20,}")
EGRESS_GUARD = "registry.example.com/platform/egress-guard@sha256:" + "5b2c" * 16
IMAGE = "registry.example.com/agents/investigator@sha256:" + "a9" * 32

AGENT = {
    "apiVersion": "agentplatform.io/v1alpha1",
    "kind": "Agent",
    "metadata": {"name": "investigator", "namespace": "tenant-a"},
    "spec": {
        "owner": "platform-team@example.com",
        "enabled": True,
        "identity": {"mode": "on-behalf-of", "onBehalfOf": {"required": True, "fallback": "deny"}},
        "capabilities": {
            "models": [
                {
                    "backend": "llm-main",
                    "models": ["Qwen3.6-35B-A3B-NVFP4"],
                    "budget": {"tokensPerDay": 5000000, "tokensPerSession": 200000},
                }
            ],
            "mcp": [
                {
                    "backend": "kubernetes",
                    "effect": "remote_read",
                    "tools": ["pods_list_in_namespace", "pods_get", "events_list"],
                    "delegation": "inherit",
                }
            ],
        },
        "runtime": {"cpu": "1", "memory": "2Gi", "ttlSeconds": 1800},
    },
}


@pytest.fixture
def cfg() -> ControllerConfig:
    return ControllerConfig(
        platform=PlatformConfig(
            issuer_url="https://idp.example.com/realms/agents",
            mcp_resource_base="https://mcp.example.com",
            gateway_cluster_ip="10.43.250.10",
            egress_guard_image=EGRESS_GUARD,
        ),
        idp_alias="kubernetes-cluster-a",
    )


def make_release(agent: dict, cfg: ControllerConfig, name: str = "investigator-r1", **spec) -> dict:
    release = {
        "apiVersion": "agentplatform.io/v1alpha1",
        "kind": "AgentRelease",
        "metadata": {"name": name, "namespace": agent["metadata"]["namespace"]},
        "spec": {
            "agent": agent["metadata"]["name"],
            "image": IMAGE,
            "manifestSha256": "4e" * 32,
            "policySha256": "",
            "approval": {"approved": True, "approvedBy": "security-review@example.com"},
        },
    }
    release["spec"]["policySha256"] = digest_hex(
        compile_bundle(
            agent,
            release,
            mcp_resource_base=cfg.platform.mcp_resource_base,
            trust_domain=cfg.platform.trust_domain,
        )
    )
    release["spec"].update(spec)
    return release


class World:
    def __init__(self, cfg: ControllerConfig) -> None:
        self.kube = FakeKube()
        self.idp = FakeIdp()
        self.verifier = FakeVerifier()
        self.cfg = cfg
        self.rec = Reconciler(
            self.kube, self.idp, self.verifier, cfg, now=lambda: "2026-10-30T12:00:00Z"
        )
        for ns in ("tenant-a", "tenant-b", "agent-platform", "mcp"):
            self.kube.put(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": ns, "namespace": None},
                }
            )
        self.kube.put(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "agent-gateway-ca", "namespace": "agent-platform"},
                "data": {
                    "ca.crt": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
                },
            }
        )

    def add(self, obj: dict) -> dict:
        return self.kube.put(copy.deepcopy(obj))

    def get(self, api_version, kind, ns, name):
        return self.kube.get(api_version, kind, ns, name)

    def agent(self, name="investigator", ns="tenant-a"):
        return self.get("agentplatform.io/v1alpha1", "Agent", ns, name)

    def release(self, name="investigator-r1", ns="tenant-a"):
        return self.get("agentplatform.io/v1alpha1", "AgentRelease", ns, name)

    def reconcile_agent(self, name="investigator", ns="tenant-a"):
        return self.rec.reconcile_agent(self.agent(name, ns))


@pytest.fixture
def world(cfg) -> World:
    return World(cfg)


@pytest.fixture(autouse=True)
def no_tokens_in_logs(caplog):
    yield
    text = caplog.text
    assert not JWT_RE.search(text) and not BEARER_RE.search(text)
