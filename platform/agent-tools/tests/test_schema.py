"""The Agent CRD's `spec` schema, as a local run reads it."""

import copy
import json
from pathlib import Path

import pytest
import yaml

from zetra_agents import schema

CRD = Path(__file__).parents[3] / "deploy/platform/crds/agents.agentplatform.io.yaml"

SPEC = {
    "owner": "dev@example.com",
    "identity": {"mode": "agent"},
    "capabilities": {
        "models": [{"backend": "llm-main", "models": ["m"], "budget": {"tokensPerSession": 1000}}],
        "mcp": [{"backend": "kubernetes", "effect": "remote_read", "tools": ["pods_list"]}],
    },
    "runtime": {"maxBoundaries": 64, "ttlSeconds": 600},
}


def test_the_packaged_schema_is_the_crds():
    (version,) = yaml.safe_load(CRD.read_text())["spec"]["versions"]
    spec = version["schema"]["openAPIV3Schema"]["properties"]["spec"]
    packaged = Path(schema.__file__).parent / "schemas" / "agent-spec.json"
    assert packaged.read_text() == json.dumps(spec, indent=2) + "\n", (
        "regenerate zetra_agents/schemas/agent-spec.json from the CRD"
    )


def test_every_keyword_the_schema_uses_is_one_this_reads():
    def keywords(node: dict) -> set[str]:
        found = set(node)
        for sub in node.get("properties", {}).values():
            found |= keywords(sub)
        if "items" in node:
            found |= keywords(node["items"])
        return found

    used = keywords(schema.agent_spec())
    assert used <= schema.CHECKED | schema.NOT_CHECKED, used - schema.CHECKED - schema.NOT_CHECKED
    assert schema.CHECKED <= used  # nothing here is read that the CRD never says

    def patterns(node: dict) -> list[str]:
        found = [node["pattern"]] if "pattern" in node else []
        for nested in (*node.get("properties", {}).values(), node.get("items")):
            found += patterns(nested) if nested else []
        return found

    # `_matches` turns a closing `$` into the end of the text; it knows no other use of one.
    assert all(p.count("$") == 1 and p.endswith("$") for p in patterns(schema.agent_spec()))


def changed(path: str, value) -> dict:
    spec = copy.deepcopy(SPEC)
    *parents, last = path.split(".")
    node = spec
    for key in parents:
        node = node[int(key)] if isinstance(node, list) else node[key]
    if value is ...:
        del node[last]
    else:
        node[last] = value
    return spec


def test_an_agent_the_cluster_stores_has_no_problems():
    assert schema.problems(SPEC) == []
    assert schema.problems(changed("runtime", ...)) == []  # defaults need no reading
    assert schema.problems({**SPEC, "notAField": 1}) == []  # the API server drops it


@pytest.mark.parametrize(
    ("path", "value", "problem"),
    [
        ("owner", ..., "spec.owner is required"),
        ("identity.mode", "user", 'spec.identity.mode: "user" is not one of agent, on-behalf-of'),
        ("runtime.maxBoundaries", 65, "spec.runtime.maxBoundaries: 65 is more than 64"),
        ("runtime.maxBoundaries", 0, "spec.runtime.maxBoundaries: 0 is less than 1"),
        ("runtime.maxBoundaries", True, "spec.runtime.maxBoundaries: true is not an integer"),
        ("runtime.maxBoundaries", "3", 'spec.runtime.maxBoundaries: "3" is not an integer'),
        ("runtime.ttlSeconds", 59, "spec.runtime.ttlSeconds: 59 is less than 60"),
        ("capabilities.mcp.0.effect", ..., "spec.capabilities.mcp[0].effect is required"),
        (
            "capabilities.mcp.0.effect",
            "remote_write",
            'spec.capabilities.mcp[0].effect: "remote_write" is not one of remote_read',
        ),
        ("capabilities.mcp.0.tools", [], "spec.capabilities.mcp[0].tools: needs at least 1 item"),
        (
            "capabilities.mcp.0.tools",
            ["has space"],
            'spec.capabilities.mcp[0].tools[0]: "has space" does not match ^[A-Za-z0-9_.-]{1,128}$',
        ),
        (
            "capabilities.mcp.0.backend",
            None,
            "spec.capabilities.mcp[0].backend: null is not a string",
        ),
        (
            "capabilities.models.0.budget.tokensPerSession",
            0,
            "spec.capabilities.models[0].budget.tokensPerSession: 0 is less than 1",
        ),
        ("capabilities", [], "spec.capabilities: [] is not an object"),
        (
            "capabilities.mcp.0.backend",
            "kubernetes\n",
            'spec.capabilities.mcp[0].backend: "kubernetes\\n" does not match ^[a-z0-9-]{1,63}$',
        ),
        ("description", "x" * 513, "spec.description: longer than 512 characters"),
        ("runtime.maxBoundaries", 3.0, "spec.runtime.maxBoundaries: 3.0 is not an integer"),
        ("runtime", None, "spec.runtime: null is not an object"),
    ],
)
def test_what_the_cluster_would_refuse_is_named(path, value, problem):
    assert schema.problems(changed(path, value)) == [problem]


def test_a_spec_that_is_not_a_mapping_is_one_problem():
    assert schema.problems(None) == ["spec: null is not an object"]
    assert schema.problems(float("inf"))[0].endswith("is not an object")
