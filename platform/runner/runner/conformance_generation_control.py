"""Exact, read-only T21 run controls. Root owns commands and test finalizers."""

from __future__ import annotations

import json
import re

from kubernetes import client

from runner.conformance_lifecycle_control import canonical_uuid

NAME = "conformance-generation-control"
NAMESPACE = "agent-platform"
FINALIZER = "agentplatform.io/t21-conformance-hold"
CASES = ("fence_only", "blocked_deletion", "recovery_control")


def validate_config(value):
    if not isinstance(value, dict) or set(value) != {
        "version",
        "run_id",
        "tenant",
        "probe_release",
        "recovery_release",
        "queue",
        "worker_image",
        "sessions",
    }:
        raise ValueError("invalid T21 run configuration")
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["tenant"] != "tenant-test"
        or not canonical_uuid(value["run_id"])
    ):
        raise ValueError("T21 requires exact tenant-test run")
    if value["queue"] != "conformance-generation-" + value["run_id"]:
        raise ValueError("T21 queue must be unique to the run")
    for key, prefix in (
        ("probe_release", "conformance-b-"),
        ("recovery_release", "conformance-checkpoint-"),
    ):
        if not isinstance(value[key], str) or not re.fullmatch(
            prefix + r"[a-z0-9-]{1,40}", value[key]
        ):
            raise ValueError("T21 requires exact existing fixture release")
    if not isinstance(value["worker_image"], str) or not re.fullmatch(
        r"registry\.almckay\.io/platform/session-runner:[a-z0-9._-]+@sha256:[0-9a-f]{64}",
        value["worker_image"],
    ):
        raise ValueError("T21 requires pinned worker image")
    sessions = value["sessions"]
    if (
        not isinstance(sessions, dict)
        or set(sessions) != set(CASES)
        or not all(canonical_uuid(s) for s in sessions.values())
        or len(set(sessions.values())) != len(CASES)
    ):
        raise ValueError("T21 requires three distinct exact sessions")
    return value


class Control:
    def __init__(self, core):
        self.core = core
        first = self._read()
        self.uid = first["metadata"].get("uid")
        self.raw_config = first.get("data", {}).get("config", "")
        if not self.uid or len(self.raw_config) > 16384:
            raise ValueError("invalid T21 control identity/bound")
        self.config = validate_config(json.loads(self.raw_config))

    def _read(self):
        value = self.core.read_namespaced_config_map(NAME, NAMESPACE, _request_timeout=5)
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
            raise ValueError("T21 control replaced or run configuration changed")
        return value

    def get(self, key):
        if key not in {*("command-" + c for c in CASES), "receipt-blocked_deletion"}:
            raise ValueError("unknown T21 control key")
        raw = self.read().get("data", {}).get(key)
        if raw is None:
            return None
        if not isinstance(raw, str) or len(raw) > 8192:
            raise ValueError("T21 control value exceeds bound")
        return json.loads(raw)

    def commanded(self, case, pod_uid, worker_uid):
        if case not in CASES:
            raise ValueError("unbound T21 case")
        value = self.get("command-" + case)
        if value is None:
            return False
        expected = {
            "run_id": self.config["run_id"],
            "session_id": self.config["sessions"][case],
            "pod_uid": pod_uid,
            "worker_uid": worker_uid,
            "action": "fence_generation_2" if case == "fence_only" else "delete_for_recovery",
        }
        if value != expected:
            raise ValueError("T21 command does not bind exact current worker/session/Pod/action")
        return True

    def finalizer_receipt(self, pod_uid):
        value = self.get("receipt-blocked_deletion")
        if value is None:
            return None
        expected = {"run_id", "session_id", "target_uid", "finalizer_added", "finalizer_removed"}
        if isinstance(value, dict) and value.get("finalizer_removed") is True:
            expected |= {"clock_boot_id", "removal_started_ns", "removal_acknowledged_ns"}
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value["run_id"] != self.config["run_id"]
            or value["session_id"] != self.config["sessions"]["blocked_deletion"]
            or value["target_uid"] != pod_uid
            or type(value["finalizer_added"]) is not bool
            or type(value["finalizer_removed"]) is not bool
        ):
            raise ValueError("invalid exact T21 finalizer receipt")
        if value["finalizer_removed"] and (
            not canonical_uuid(value["clock_boot_id"])
            or type(value["removal_started_ns"]) is not int
            or type(value["removal_acknowledged_ns"]) is not int
            or not 0 < value["removal_started_ns"] <= value["removal_acknowledged_ns"]
        ):
            raise ValueError("T21 removal receipt lacks exact monotonic clock bounds")
        return value
