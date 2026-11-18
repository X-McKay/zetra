"""The three JSON Patch operations the contract's case tables use."""

from __future__ import annotations

import copy
from typing import Any


def patched(document: Any, operations: list[dict[str, Any]]) -> Any:
    """A copy of `document` with add, replace and remove applied (JSON Pointer paths)."""
    result = copy.deepcopy(document)
    for operation in operations:
        parts = [p.replace("~1", "/").replace("~0", "~") for p in operation["path"].split("/")[1:]]
        if not parts:
            if operation["op"] == "remove":
                return None
            result = copy.deepcopy(operation["value"])
            continue
        parent = result
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        last = parts[-1]
        if isinstance(parent, list):
            index = len(parent) if last == "-" else int(last)
            if operation["op"] == "add":
                parent.insert(index, copy.deepcopy(operation["value"]))
            elif operation["op"] == "replace":
                parent[index] = copy.deepcopy(operation["value"])
            else:
                del parent[index]
        elif operation["op"] == "remove":
            del parent[last]
        else:
            if operation["op"] == "replace" and last not in parent:
                raise KeyError(operation["path"])
            parent[last] = copy.deepcopy(operation["value"])
    return result
