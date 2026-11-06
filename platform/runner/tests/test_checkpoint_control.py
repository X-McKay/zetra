import copy
import json

import pytest

from runner.conformance_checkpoint_control import Control, ConfigConflict, validate_config

RUN = "4bc9beea-e690-43eb-bf58-211044970094"
CASES = ("control-before", "a", "b", "c", "d", "e", "control-after")


def config():
    return {
        "version": 1,
        "run_id": RUN,
        "tenant": "tenant-test",
        "agent": "conformance-checkpoint",
        "release": "checkpoint-signed",
        "queue": "conformance-checkpoint-" + RUN,
        "sessions": {case: f"00000000-0000-4000-8000-{i:012d}" for i, case in enumerate(CASES, 1)},
    }


class Core:
    def __init__(self):
        self.value = {
            "metadata": {"uid": "original-cm", "resourceVersion": "1"},
            "data": {"config": json.dumps(config()), "unrelated": "preserve"},
        }
        self.patches = []

    def read_namespaced_config_map(self, name, namespace, **kwargs):
        assert (name, namespace) == ("conformance-checkpoint-control", "agent-platform")
        return copy.deepcopy(self.value)

    def patch_namespaced_config_map(self, name, namespace, patch, **kwargs):
        self.patches.append(patch)
        for operation in patch:
            key, leaf = operation["path"].strip("/").split("/")
            if operation["op"] == "test":
                if self.value[key].get(leaf) != operation["value"]:
                    raise ConfigConflict("concurrent change")
            else:
                self.value[key][leaf] = operation["value"]
        self.value["metadata"]["resourceVersion"] = str(
            int(self.value["metadata"]["resourceVersion"]) + 1
        )
        return copy.deepcopy(self.value)


@pytest.mark.parametrize(
    "field, value",
    [
        ("tenant", "tenant-prod"),
        ("agent", "investigator"),
        ("queue", "sessions-tenant-test"),
        ("sessions", {"a": RUN}),
        ("version", 2),
    ],
)
def test_configuration_never_accepts_unbounded_identity(field, value):
    bad = dict(config(), **{field: value})
    with pytest.raises(ValueError):
        validate_config(bad)


def test_patch_preserves_unrelated_fields_and_pins_original_uid():
    core = Core()
    control = Control(core)
    control.patch("command", None, {"gate_id": RUN, "run_id": RUN, "action": "kill"})
    assert core.value["data"]["unrelated"] == "preserve"
    assert core.patches[0][0] == {"op": "test", "path": "/metadata/uid", "value": "original-cm"}
    original = copy.deepcopy(core.value)
    core.value["metadata"]["uid"] = "replacement-cm"
    with pytest.raises(ConfigConflict):
        control.patch("command", json.loads(original["data"]["command"]), {"action": "abort"})
    assert core.value["data"] == original["data"]


def test_concurrent_key_or_config_change_is_not_overwritten():
    core = Core()
    control = Control(core)
    core.value["data"]["command"] = json.dumps({"action": "other"})
    with pytest.raises(ConfigConflict):
        control.patch("command", None, {"action": "kill"})
    core.value["data"]["config"] = json.dumps(dict(config(), release="other"))
    with pytest.raises(ConfigConflict):
        control.read()
    assert core.patches == []
