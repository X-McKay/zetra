"""Read-only, exact-run T24 operator controls; no workload gets patch rights."""

from __future__ import annotations

import json
import re
import uuid

from kubernetes import client

NAME = "conformance-lifecycle-control"
NAMESPACE = "agent-platform"
FAULTS = ("aps_down", "finalizer", "worker_kill", "terminate")
SUPPLEMENTAL = ("success", "cancel", "activity_crash", "retry_exhaustion")
CASES = ("success", *FAULTS, "cancel", "activity_crash", "retry_exhaustion")


def canonical_uuid(value):
    try:
        parsed = uuid.UUID(value)
        return parsed.version == 4 and str(parsed) == value
    except (ValueError, TypeError, AttributeError):
        return False


def validate_config(value):
    if not isinstance(value, dict) or set(value) != {
        "version",
        "run_id",
        "tenant",
        "agent",
        "release",
        "queue",
        "worker_image",
        "sessions",
    }:
        raise ValueError("invalid lifecycle run configuration")
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["tenant"] != "tenant-test"
        or value["agent"] != "conformance-lifecycle"
    ):
        raise ValueError("lifecycle fixture identity is not allowed")
    if (
        not canonical_uuid(value["run_id"])
        or value["queue"] != "conformance-lifecycle-" + value["run_id"]
    ):
        raise ValueError("lifecycle queue must be unique to this run")
    if not isinstance(value["release"], str) or not re.fullmatch(
        r"conformance-lifecycle-[a-z0-9-]{1,41}", value["release"]
    ):
        raise ValueError("invalid lifecycle release")
    if not isinstance(value["worker_image"], str) or not re.fullmatch(
        r"registry\.almckay\.io/platform/session-runner:[a-z0-9._-]+@sha256:[0-9a-f]{64}",
        value["worker_image"],
    ):
        raise ValueError("lifecycle worker requires an exact published image")
    sessions = value["sessions"]
    if (
        not isinstance(sessions, dict)
        or set(sessions) != set(CASES)
        or not all(canonical_uuid(s) for s in sessions.values())
        or len(set(sessions.values())) != len(CASES)
    ):
        raise ValueError("lifecycle cases require unique exact session ids")
    return value


class Control:
    def __init__(self, core):
        self.core = core
        first = self._read()
        self.uid = first["metadata"].get("uid")
        if not self.uid:
            raise ValueError("lifecycle control has no UID")
        self.raw_config = first["data"]["config"]
        if len(self.raw_config) > 16384:
            raise ValueError("lifecycle config exceeds bound")
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
            raise ValueError("lifecycle control identity or configuration changed")
        return value

    def get(self, key):
        if key not in {
            *("receipt-" + c for c in CASES),
            *("grant-" + c for c in CASES),
            *("release-" + c + "-boundary1" for c in CASES),
        }:
            raise ValueError("unrecognized lifecycle control key")
        raw = self.read().get("data", {}).get(key)
        if raw is None:
            return None
        if not isinstance(raw, str) or len(raw) > 8192:
            raise ValueError("lifecycle control value exceeds bound")
        return json.loads(raw)

    def released(self, case, stage, generation):
        if case not in CASES or stage != "boundary1" or generation != 1:
            raise ValueError("unrecognized lifecycle release gate")
        value = self.get("release-" + case + "-" + stage)
        if value is None:
            return False
        expected = {
            "session_id": self.config["sessions"][case],
            "stage": stage,
            "generation": generation,
        }
        if value != expected or type(value.get("generation")) is not int:
            raise ValueError("operator release is not bound to this lifecycle gate")
        return True
