"""The defaults of the Agent resource, filled in the way the API server fills them.

A manifest may leave out a field that has a default; the object on the cluster then has it.
The registry keeps and compares the Agent as the cluster holds it, so every Agent document is
passed through `agent_spec` before it is kept (`canonical.agent_document`). The AgentRelease
resource has no defaults.

`agent-spec.schema.json` is the `spec` schema of `deploy/platform/crds/agents.agentplatform.io.yaml`;
a test fails when the two differ.
"""

from __future__ import annotations

import copy
import json
from functools import cache
from pathlib import Path
from typing import Any

SCHEMA = Path(__file__).with_name("agent-spec.schema.json")


@cache
def agent_spec_schema() -> dict[str, Any]:
    return json.loads(SCHEMA.read_text())


def with_defaults(value: Any, schema: dict[str, Any]) -> Any:
    """A copy of `value` with the schema's defaults filled in: a property that has a default
    and is missing from an object that is present gets it; an object that is missing gets
    nothing."""
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        result = {
            key: with_defaults(item, properties.get(key) or {}) for key, item in value.items()
        }
        for key, prop in properties.items():
            if "default" in prop and result.get(key) is None:
                result[key] = copy.deepcopy(prop["default"])
        return result
    if isinstance(value, list):
        return [with_defaults(item, schema.get("items") or {}) for item in value]
    return copy.deepcopy(value)


def agent_spec(spec: dict[str, Any] | None) -> dict[str, Any]:
    return with_defaults(spec or {}, agent_spec_schema())
