"""The carried declaration has the schema of the Agent's spec, without `enabled`."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

CRDS = Path(__file__).parents[3] / "deploy" / "platform" / "crds"


def spec_schema(name: str) -> dict:
    crd = yaml.safe_load((CRDS / name).read_text())
    (version,) = crd["spec"]["versions"]
    return version["schema"]["openAPIV3Schema"]["properties"]["spec"]


def without_defaults(node):
    """AgentRelease documents carry no defaults: the registry has filled them in already."""
    if isinstance(node, dict):
        return {k: without_defaults(v) for k, v in node.items() if k != "default"}
    if isinstance(node, list):
        return [without_defaults(v) for v in node]
    return node


def test_declaration_schema_equals_agent_spec_schema_without_enabled():
    agent = copy.deepcopy(spec_schema("agents.agentplatform.io.yaml"))
    del agent["properties"]["enabled"]
    declaration = spec_schema("agentreleases.agentplatform.io.yaml")["properties"]["declaration"]
    declaration = {k: v for k, v in declaration.items() if k != "description"}
    assert declaration == without_defaults(agent)
