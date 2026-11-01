"""Explicit offline tool fixtures. Fixture bindings never open a network transport."""

import hashlib
import json
import re

from ._runtime.records import canonical_bytes


def mcp_request(backend, tool, arguments):
    if (
        not isinstance(backend, str)
        or not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", backend)
        or not isinstance(tool, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", tool)
        or not isinstance(arguments, dict)
    ):
        raise ValueError("invalid MCP request")
    encoded = canonical_bytes(arguments)
    if len(encoded) > 32768:
        raise ValueError("MCP arguments exceed 32 KiB")
    return canonical_bytes([backend, tool, arguments])


class MCPFixtures:
    def __init__(self, value):
        self._data = canonical_bytes(value)
        if len(self._data) > 1_048_576:
            raise ValueError("MCP fixtures exceed 1 MiB")
        data = json.loads(self._data)
        if (
            not isinstance(data, dict)
            or set(data) != {"schema_version", "calls"}
            or type(data["schema_version"]) is not int
            or data["schema_version"] != 1
            or not isinstance(data["calls"], list)
            or not 1 <= len(data["calls"]) <= 256
        ):
            raise ValueError("invalid MCP fixture document")
        self._results = {}
        for item in data["calls"]:
            if not isinstance(item, dict) or set(item) != {
                "backend",
                "tool",
                "arguments",
                "result",
            }:
                raise ValueError("invalid MCP fixture call")
            key = mcp_request(item["backend"], item["tool"], item["arguments"])
            result = canonical_bytes(item["result"])
            if key in self._results or len(result) > 65536:
                raise ValueError("duplicate or oversized MCP fixture")
            self._results[key] = result

    def config(self):
        return json.loads(self._data)

    def provenance(self):
        return {"mode": "fixture", "sha256": hashlib.sha256(self._data).hexdigest()}

    async def call(self, backend, tool, arguments):
        key = mcp_request(backend, tool, arguments)
        if key not in self._results:
            raise ValueError("MCP call has no exact fixture")
        return json.loads(self._results[key])


def mcp_provenance(fixtures):
    return fixtures.provenance() if fixtures else {"mode": "unbound", "sha256": None}
