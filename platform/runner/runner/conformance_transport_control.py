"""Exact immutable run binding and operator receipts for T27; read-only cluster API."""

from __future__ import annotations

import ipaddress
import json
import math
import re
import uuid

from kubernetes import client

NAME = "conformance-transport-control"
NAMESPACE = "agent-platform"
GATE_FIELDS = {"run_id", "session_id", "gate_id", "pod", "pod_uid", "pod_ip", "node", "worker_uid"}
CAPTURE_FIELDS = {
    "interface",
    "ifindex",
    "peer_ifindex",
    "netns_inode",
    "started_at",
    "pid",
    "filter",
    "max_seconds",
    "max_packets",
    "max_bytes",
}


def canonical_uuid(value):
    try:
        u = uuid.UUID(value)
        return u.version == 4 and str(u) == value
    except (TypeError, ValueError, AttributeError):
        return False


def capture_filter(pod_ip):
    ip = str(ipaddress.IPv4Address(pod_ip))
    if not ip.startswith("10.42."):
        raise ValueError("capture requires a tenant Pod address")
    return f"host {ip} and host 10.43.250.10 and tcp port 443"


def validate_config(value):
    required = {
        "version",
        "run_id",
        "tenant",
        "queue",
        "worker_image",
        "transport_release",
        "t4_release",
        "sessions",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid transport run configuration")
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["tenant"] != "tenant-test"
        or not canonical_uuid(value["run_id"])
        or value["queue"] != "conformance-transport-" + value["run_id"]
    ):
        raise ValueError("transport run identity is not allowed")
    for key, prefix in [
        ("transport_release", "conformance-transport-"),
        ("t4_release", "conformance-"),
    ]:
        if not isinstance(value[key], str) or not re.fullmatch(
            re.escape(prefix) + r"[a-z0-9-]{1,41}", value[key]
        ):
            raise ValueError("transport release is not bound")
    if not isinstance(value["worker_image"], str) or not re.fullmatch(
        r"registry\.almckay\.io/platform/session-runner:[a-z0-9._-]+@sha256:[a-f0-9]{64}",
        value["worker_image"],
    ):
        raise ValueError("transport worker image must be immutable")
    sessions = value["sessions"]
    if (
        not isinstance(sessions, dict)
        or set(sessions) != {"transport", "t4"}
        or not all(canonical_uuid(s) for s in sessions.values())
        or len(set(sessions.values())) != 2
    ):
        raise ValueError("transport sessions must have distinct exact UUIDs")
    return value


def validate_gate(gate):
    if not isinstance(gate, dict) or set(gate) != GATE_FIELDS:
        raise ValueError("invalid transport gate schema")
    if (
        any(
            not canonical_uuid(gate[k])
            for k in ("run_id", "session_id", "gate_id", "pod_uid", "worker_uid")
        )
        or not re.fullmatch(r"s-[0-9a-f]{16}-g1", gate["pod"])
        or gate["node"] != "rig0"
    ):
        raise ValueError("transport gate identity is invalid")
    capture_filter(gate["pod_ip"])
    return gate


def capture_started(value, gate):
    if value is None:
        return False
    validate_gate(gate)
    if (
        not isinstance(value, dict)
        or set(value) != GATE_FIELDS | CAPTURE_FIELDS
        or any(value.get(k) != v for k, v in gate.items())
    ):
        raise ValueError("operator capture does not match exact gate")
    if not isinstance(value["interface"], str) or not re.fullmatch(
        r"veth[a-zA-Z0-9]{1,11}", value["interface"]
    ):
        raise ValueError("capture requires a single veth interface")
    if (
        any(
            type(value[k]) is not int or value[k] <= 0
            for k in ("ifindex", "peer_ifindex", "netns_inode", "pid")
        )
        or type(value["started_at"]) not in (int, float)
        or not math.isfinite(value["started_at"])
        or value["started_at"] <= 0
    ):
        raise ValueError("invalid capture process/interface metadata")
    if value["filter"] != capture_filter(gate["pod_ip"]) or (
        value["max_seconds"],
        value["max_packets"],
        value["max_bytes"],
    ) != (120, 4096, 33554432):
        raise ValueError("capture limits or filter differ from reviewed scope")
    return True


class Control:
    def __init__(self, core):
        self.core = core
        first = self._read()
        self.uid = first["metadata"].get("uid")
        self.raw_config = first.get("data", {}).get("config")
        if not self.uid or not isinstance(self.raw_config, str) or len(self.raw_config) > 16384:
            raise ValueError("transport control identity/configuration missing")
        self.config = validate_config(json.loads(self.raw_config))
        self.observed = {}

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
            raise ValueError("transport control identity/configuration changed")
        for key, raw in self.observed.items():
            if value.get("data", {}).get(key) != raw:
                raise ValueError("operator removed or changed a transport receipt")
        return value

    def get(self, key):
        if key not in {"capture-start", "capture-complete"}:
            raise ValueError("unknown transport receipt")
        raw = self.read().get("data", {}).get(key)
        if raw is None:
            return None
        if not isinstance(raw, str) or len(raw) > 16384:
            raise ValueError("transport receipt exceeds bound")
        self.observed[key] = raw
        return json.loads(raw)
