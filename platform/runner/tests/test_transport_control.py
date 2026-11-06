import copy
import json
from types import SimpleNamespace

import pytest

from runner.conformance_transport_control import Control, validate_config, capture_started

RUN = "b2315284-930a-4ec2-8b67-61e8e2e63a76"
T4 = "fcd86914-1293-4a30-874c-74325d7acdb5"
TR = "bc9a5bcc-80a9-4398-b91f-35c90aad33a3"


def config():
    return {
        "version": 1,
        "run_id": RUN,
        "tenant": "tenant-test",
        "queue": "conformance-transport-" + RUN,
        "worker_image": "registry.almckay.io/platform/session-runner:test@sha256:" + "a" * 64,
        "transport_release": "conformance-transport-signed",
        "t4_release": "conformance-signed",
        "sessions": {"transport": TR, "t4": T4},
    }


def test_control_pins_uid_and_exact_config_without_write_api():
    doc = {"metadata": {"uid": "cmuid"}, "data": {"config": json.dumps(config())}}
    core = SimpleNamespace(read_namespaced_config_map=lambda *a, **k: copy.deepcopy(doc))
    c = Control(core)
    assert c.get("capture-start") is None
    doc["metadata"]["uid"] = "replacement"
    with pytest.raises(ValueError):
        c.read()


@pytest.mark.parametrize(
    "key,value",
    [
        ("tenant", "tenant-prod"),
        ("queue", "production"),
        ("worker_image", "mutable:latest"),
        ("version", True),
    ],
)
def test_config_rejects_unbound_identity(key, value):
    value_config = config()
    value_config[key] = value
    with pytest.raises(ValueError):
        validate_config(value_config)


def gate():
    return {
        "run_id": RUN,
        "session_id": T4,
        "gate_id": "c082dbde-e67f-4e91-b99f-ec58a764f9bb",
        "pod": "s-0123456789abcdef-g1",
        "pod_uid": "125ab5cc-c6fc-4f1f-89cd-55012fa12e3b",
        "pod_ip": "10.42.1.17",
        "node": "rig0",
        "worker_uid": "db0c9a0c-5d95-465c-a062-f1a84fc7b861",
    }


def receipt():
    return {
        **gate(),
        "interface": "vethabcdef",
        "ifindex": 55,
        "peer_ifindex": 2,
        "netns_inode": 4002,
        "started_at": 1793990000.0,
        "pid": 12345,
        "filter": "host 10.42.1.17 and host 10.43.250.10 and tcp port 443",
        "max_seconds": 120,
        "max_packets": 4096,
        "max_bytes": 33554432,
    }


def test_capture_release_requires_exact_gate_and_bounded_interface_receipt():
    assert capture_started(receipt(), gate())
    assert not capture_started(None, gate())
    for key, value in [
        ("pod_uid", "other"),
        ("session_id", TR),
        ("gate_id", RUN),
        ("interface", "any"),
        ("ifindex", 0),
        ("max_seconds", 3600),
        ("filter", "tcp"),
        ("started_at", float("nan")),
    ]:
        changed = receipt()
        changed[key] = value
        with pytest.raises(ValueError):
            capture_started(changed, gate())
