import copy
import json
import uuid

import pytest

from runner.conformance_lifecycle_control import CASES, Control, validate_config


def configuration():
    run = str(uuid.uuid4())
    return {
        "version": 1,
        "run_id": run,
        "tenant": "tenant-test",
        "agent": "conformance-lifecycle",
        "release": "conformance-lifecycle-2026-11-06-aabbccdd",
        "queue": "conformance-lifecycle-" + run,
        "worker_image": "registry.almckay.io/platform/session-runner:test@sha256:" + "a" * 64,
        "sessions": {case: str(uuid.uuid4()) for case in CASES},
    }


class Core:
    def __init__(self):
        self.doc = {
            "metadata": {"uid": "control-uid"},
            "data": {"config": json.dumps(configuration())},
        }

    def read_namespaced_config_map(self, name, namespace, **kwargs):
        assert (name, namespace) == ("conformance-lifecycle-control", "agent-platform")
        return copy.deepcopy(self.doc)


def test_exact_read_only_control_accepts_only_bound_release():
    core = Core()
    control = Control(core)
    case = "aps_down"
    assert not control.released(case, "boundary1", 1)
    core.doc["data"]["release-aps_down-boundary1"] = json.dumps(
        {
            "session_id": control.config["sessions"][case],
            "stage": "boundary1",
            "generation": 1,
        }
    )
    assert control.released(case, "boundary1", 1)
    with pytest.raises(ValueError):
        control.released(case, "boundary1", 2)
    assert not hasattr(control, "patch")


@pytest.mark.parametrize(
    "mutation", ["uid", "config", "release_session", "extra_field", "oversized"]
)
def test_replaced_drifted_or_unbound_control_fails(mutation):
    core = Core()
    c = Control(core)
    value = {"session_id": c.config["sessions"]["success"], "stage": "boundary1", "generation": 1}
    if mutation == "uid":
        core.doc["metadata"]["uid"] = "replacement"
    elif mutation == "config":
        core.doc["data"]["config"] += " "
    elif mutation == "release_session":
        value["session_id"] = str(uuid.uuid4())
    elif mutation == "extra_field":
        value["credential"] = "must never be reflected"
    else:
        value["stage"] = "x" * 8193
    core.doc["data"]["release-success-boundary1"] = json.dumps(value)
    with pytest.raises(ValueError):
        c.released("success", "boundary1", 1)


@pytest.mark.parametrize(
    "mutation",
    ["agent", "tenant", "queue", "duplicate_session", "extra_case", "image", "bool_version"],
)
def test_configuration_cannot_select_other_work(mutation):
    value = configuration()
    if mutation in ("agent", "tenant", "queue", "image"):
        value["worker_image" if mutation == "image" else mutation] = "untrusted"
    elif mutation == "duplicate_session":
        value["sessions"]["success"] = value["sessions"]["cancel"]
    elif mutation == "extra_case":
        value["sessions"]["other"] = str(uuid.uuid4())
    else:
        value["version"] = True
    with pytest.raises(ValueError):
        validate_config(value)
