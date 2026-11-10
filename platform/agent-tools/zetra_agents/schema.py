"""What the Agent CRD admits in `spec`.

Kubani's API server refuses an Agent outside this schema when Flux applies it. A local
run holds `deploy/agent.yaml` to the same schema, so a limit the cluster would never store
(sixty-five boundaries, a budget of zero) cannot shape a session here.

`schemas/agent-spec.json` is the `spec` schema of zetra
`deploy/platform/crds/agents.agentplatform.io.yaml`; a test fails when they differ or when
the CRD starts using a keyword this does not read. `default` needs no reading.

This is not the API server's validator, and it differs from it in known ways. `format`
(the owner's `email`) is not checked, and neither is an integer past 64 bits, which the
platform cannot carry. The file is read as YAML 1.1 with the last of a repeated key
winning, where the tools that apply it to the cluster read YAML 1.2 and refuse a repeated
key, so `enabled: yes` is a boolean here and a string there. In the other direction
this is stricter: a null and an integral float such as `3.0` are refused here where the
cluster would drop the one and accept the other. An unknown field is not a problem here:
the API server drops it. Where the two differ, the cluster has the last word.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any

CHECKED = {
    "type",
    "properties",
    "required",
    "items",
    "minItems",
    "minimum",
    "maximum",
    "maxLength",
    "pattern",
    "enum",
}
NOT_CHECKED = {"default", "format"}

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
}


@functools.cache
def agent_spec() -> dict:
    path = Path(__file__).parent / "schemas" / "agent-spec.json"
    return json.loads(path.read_text(encoding="utf-8"))


def problems(spec: Any) -> list[str]:
    """Why the API server would refuse an Agent with this `spec`; empty when it would not."""
    return _problems(spec, agent_spec(), "spec")


def _matches(pattern: str, value: str) -> bool:
    """As the API server's RE2 reads it: `$` is the end of the text, where Python's also
    matches before a final newline."""
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + "\\Z"
    return re.search(pattern, value) is not None


def _show(value: Any) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= 60 else text[:57] + "..."


def _problems(value: Any, schema: dict, where: str) -> list[str]:
    kind = schema["type"]
    if not _TYPES[kind](value):
        article = "an" if kind[0] in "aeio" else "a"
        return [f"{where}: {_show(value)} is not {article} {kind}"]
    found: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{where}: {_show(value)} is not one of {', '.join(schema['enum'])}")
    if "pattern" in schema and not _matches(schema["pattern"], value):
        found.append(f"{where}: {_show(value)} does not match {schema['pattern']}")
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        found.append(f"{where}: longer than {schema['maxLength']} characters")
    if "minimum" in schema and value < schema["minimum"]:
        found.append(f"{where}: {_show(value)} is less than {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        found.append(f"{where}: {_show(value)} is more than {schema['maximum']}")
    if "minItems" in schema and len(value) < schema["minItems"]:
        found.append(f"{where}: needs at least {schema['minItems']} item")
    if kind == "array":
        for index, item in enumerate(value):
            found += _problems(item, schema["items"], f"{where}[{index}]")
    if kind == "object":
        found += [
            f"{where}.{key} is required" for key in schema.get("required", ()) if key not in value
        ]
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                found += _problems(value[key], sub, f"{where}.{key}")
    return found
