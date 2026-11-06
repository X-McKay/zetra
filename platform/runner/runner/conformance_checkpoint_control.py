"""Exact, temporary T23 control object shared by the harness and test worker.

The operator creates this object and its resourceName-scoped RBAC. Neither role
may replace it, change its run binding, or overwrite another writer's value.
"""

from __future__ import annotations

import json
import re
import uuid

from kubernetes import client

NAME = "conformance-checkpoint-control"
NAMESPACE = "agent-platform"
CASES = ("control-before", "a", "b", "c", "d", "e", "control-after")


class ConfigConflict(ValueError):
    pass


def validate_config(value):
    if not isinstance(value, dict) or set(value) != {
        "version",
        "run_id",
        "tenant",
        "agent",
        "release",
        "queue",
        "sessions",
    }:
        raise ValueError("invalid checkpoint run configuration")
    if (
        value["version"] != 1
        or value["tenant"] != "tenant-test"
        or value["agent"] != "conformance-checkpoint"
    ):
        raise ValueError("checkpoint fixture identity is not allowed")
    if str(uuid.UUID(value["run_id"])) != value["run_id"]:
        raise ValueError("invalid checkpoint run id")
    if value["queue"] != "conformance-checkpoint-" + value["run_id"]:
        raise ValueError("checkpoint queue must be unique to the run")
    if not isinstance(value["release"], str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,62}", value["release"]
    ):
        raise ValueError("invalid checkpoint release")
    sessions = value["sessions"]
    if (
        not isinstance(sessions, dict)
        or set(sessions) != set(CASES)
        or len(set(sessions.values())) != len(CASES)
    ):
        raise ValueError("checkpoint cases require unique exact session ids")
    for session in sessions.values():
        if str(uuid.UUID(session)) != session:
            raise ValueError("invalid checkpoint session id")
    return value


class Control:
    def __init__(self, core):
        self.core = core
        first = self._read()
        self.uid = first["metadata"]["uid"]
        if not self.uid:
            raise ValueError("checkpoint control has no UID")
        self.raw_config = first["data"]["config"]
        self.config = validate_config(json.loads(self.raw_config))

    def _read(self):
        value = self.core.read_namespaced_config_map(NAME, NAMESPACE, _request_timeout=10)
        return (
            value
            if isinstance(value, dict)
            else client.ApiClient().sanitize_for_serialization(value)
        )

    def read(self):
        value = self._read()
        if (
            value["metadata"].get("uid") != self.uid
            or value.get("data", {}).get("config") != self.raw_config
        ):
            raise ConfigConflict("checkpoint control identity or configuration changed")
        return value

    def get(self, key):
        raw = self.read().get("data", {}).get(key)
        return json.loads(raw) if raw is not None else None

    def patch(self, key, expected, value):
        if key not in {"command", "gate", *("receipt-" + case for case in CASES)}:
            raise ValueError("checkpoint control key is not writable")
        current = self.read()
        raw = current.get("data", {}).get(key)
        if (json.loads(raw) if raw is not None else None) != expected:
            raise ConfigConflict("checkpoint control field changed concurrently")
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if len(encoded) > 8192:
            raise ValueError("checkpoint control value exceeds bound")
        # resourceVersion protects an absent key too; no full-object replacement.
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": self.uid},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": current["metadata"]["resourceVersion"],
            },
            {"op": "test", "path": "/data/config", "value": self.raw_config},
            {"op": "add", "path": "/data/" + key, "value": encoded},
        ]
        self.core.patch_namespaced_config_map(NAME, NAMESPACE, patch, _request_timeout=10)
        final = self.read()
        if final["data"].get(key) != encoded:
            raise ConfigConflict("checkpoint control write was not confirmed")
        return value
