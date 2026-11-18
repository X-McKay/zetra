"""An Agent is kept as the cluster holds it: with the resource's defaults filled in."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from agent_registry import canonical, defaults
from tests.world import fixture_documents

CRDS = Path(__file__).parents[3] / "deploy" / "platform" / "crds"


def schema_of(name: str) -> dict:
    crd = yaml.safe_load((CRDS / name).read_text())
    (version,) = crd["spec"]["versions"]
    return version["schema"]["openAPIV3Schema"]


def test_the_schema_kept_here_is_the_one_the_cluster_uses():
    # After a change to the Agent resource, copy its `spec` schema into
    # agent_registry/agent-spec.schema.json (JSON, sorted keys).
    on_cluster = schema_of("agents.agentplatform.io.yaml")["properties"]["spec"]

    assert defaults.agent_spec_schema() == on_cluster


def test_an_agent_release_has_no_defaults():
    # If one is added, AgentRelease documents need the same treatment as Agent documents.
    assert "default:" not in (CRDS / "agentreleases.agentplatform.io.yaml").read_text()


def test_the_fixture_agent_gets_what_the_cluster_would_add():
    agent, _ = fixture_documents()

    spec = defaults.agent_spec(agent["spec"])

    assert spec["enabled"] is True
    assert spec["runtime"] == {
        "cpu": "1",
        "memory": "2Gi",
        "ttlSeconds": 900,
        "runtimeClass": "kata-qemu-runtime-rs",
        "maxBoundaries": 64,
    }
    assert spec["capabilities"] == agent["spec"]["capabilities"]
    assert "enabled" not in agent["spec"]  # the argument is not changed
    assert defaults.agent_spec(spec) == spec


def test_a_missing_object_gets_nothing_and_a_present_one_is_completed():
    spec = {
        "identity": {"mode": "on-behalf-of", "onBehalfOf": {}},
        "capabilities": {
            "models": [{"backend": "llm-main", "models": ["m"], "budget": {}}, {"backend": "b"}],
            "mcp": [{"backend": "tools", "tools": ["t"]}],
        },
    }

    filled = defaults.agent_spec(spec)

    assert "runtime" not in filled
    assert filled["identity"]["onBehalfOf"] == {"required": True, "fallback": "deny"}
    assert filled["capabilities"]["models"][0]["budget"] == {
        "tokensPerDay": 2000000,
        "tokensPerSession": 200000,
    }
    assert "budget" not in filled["capabilities"]["models"][1]
    assert filled["capabilities"]["mcp"][0]["delegation"] == "inherit"
    assert defaults.agent_spec({"enabled": False})["enabled"] is False
    assert defaults.agent_spec(None) == {"enabled": True}


def test_a_manifest_and_the_object_on_the_cluster_are_the_same_agent():
    manifest, _ = fixture_documents()
    live = copy.deepcopy(manifest)
    live["spec"] = defaults.agent_spec(manifest["spec"])
    live["metadata"]["uid"] = "u"

    assert canonical.agent_document(manifest) == canonical.agent_document(live)
    assert canonical.authority_of(manifest) == canonical.authority_of(live)
