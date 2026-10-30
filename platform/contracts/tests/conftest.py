from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from agent_platform.constants import PlatformConfig

FIXTURES = Path(__file__).parent / "fixtures"
EGRESS_GUARD = "registry.example.com/platform/egress-guard@sha256:" + "5b2c" * 16


@pytest.fixture
def cfg() -> PlatformConfig:
    return PlatformConfig(
        issuer_url="https://idp.example.com/realms/agents",
        mcp_resource_base="https://mcp.example.com",
        gateway_cluster_ip="10.43.250.10",
        egress_guard_image=EGRESS_GUARD,
    )


@pytest.fixture
def agent() -> dict:
    return copy.deepcopy(yaml.safe_load((FIXTURES / "agent.yaml").read_text()))


@pytest.fixture
def release() -> dict:
    return copy.deepcopy(yaml.safe_load((FIXTURES / "release.yaml").read_text()))
