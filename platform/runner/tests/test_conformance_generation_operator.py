import copy
import json
from types import SimpleNamespace as NS

import pytest

from runner.activities import pod_name
from runner.conformance_generation_control import FINALIZER, Control
from runner.conformance_generation_operator import FinalizerGuard
from tests.test_conformance_generation import config


class Timer:
    def __init__(self, seconds, function):
        self.seconds, self.function = seconds, function

    def start(self):
        pass

    def cancel(self):
        pass

    def join(self):
        pass


class Core:
    def __init__(self):
        self.api_client = NS(configuration=NS(retries=0))
        self.cfg = config()
        sid = self.cfg["sessions"]["blocked_deletion"]
        self.doc = {
            "metadata": {"uid": "control", "resourceVersion": "1"},
            "data": {"config": json.dumps(self.cfg)},
        }
        self.pod = NS(
            metadata=NS(
                uid="pod-uid",
                resource_version="1",
                labels={
                    "agentplatform.io/" + k: v
                    for k, v in {
                        "session": sid,
                        "generation": "1",
                        "agent": "conformance-checkpoint",
                        "release": self.cfg["recovery_release"],
                        "tenant": "tenant-test",
                        "sandbox": "true",
                    }.items()
                },
                deletion_timestamp=None,
                finalizers=[],
            ),
            status=NS(phase="Running"),
        )
        self.patches = []
        self.fail_add = False

    def read_namespaced_config_map(self, *a, **kw):
        return copy.deepcopy(self.doc)

    def read_namespaced_pod(self, *a, **kw):
        return copy.deepcopy(self.pod)

    def patch_namespaced_pod(self, name, namespace, patch, **kw):
        assert (
            name == pod_name(self.cfg["sessions"]["blocked_deletion"], 1)
            and namespace == "tenant-test"
        )
        assert patch[0] == {"op": "test", "path": "/metadata/uid", "value": self.pod.metadata.uid}
        assert patch[1]["value"] == self.pod.metadata.resource_version
        self.patches.append(patch)
        self.pod.metadata.finalizers = patch[2]["value"]
        self.pod.metadata.resource_version = str(int(self.pod.metadata.resource_version) + 1)
        if self.fail_add and patch[2]["value"] == [FINALIZER]:
            raise TimeoutError()

    def patch_namespaced_config_map(self, name, namespace, patch, **kw):
        assert (
            patch[0]["value"] == self.doc["metadata"]["uid"]
            and patch[1]["value"] == self.doc["metadata"]["resourceVersion"]
        )
        self.doc["data"][patch[2]["path"].split("/")[-1]] = patch[2]["value"]
        self.doc["metadata"]["resourceVersion"] = str(
            int(self.doc["metadata"]["resourceVersion"]) + 1
        )


def guard(core):
    ticks = iter([100, 200])
    return FinalizerGuard(
        core,
        Control(core),
        "pod-uid",
        "worker-uid",
        timer_factory=Timer,
        now_ns=lambda: next(ticks),
        boot_id=core.cfg["run_id"],
    )


def test_context_always_removes_exact_finalizer_and_records_root_clock_bounds():
    core = Core()
    with guard(core):
        assert core.pod.metadata.finalizers == [FINALIZER]
        command = json.loads(core.doc["data"]["command-blocked_deletion"])
        assert command["pod_uid"] == "pod-uid" and command["worker_uid"] == "worker-uid"
    assert core.pod.metadata.finalizers == []
    receipt = Control(core).finalizer_receipt("pod-uid")
    assert receipt["finalizer_added"] and receipt["finalizer_removed"]
    assert (receipt["removal_started_ns"], receipt["removal_acknowledged_ns"]) == (100, 200)


def test_independent_timer_removes_hold_even_without_any_scheduled_gate():
    core = Core()
    with pytest.raises(TimeoutError, match="hold expired"):
        with guard(core) as active:
            assert active.timer.seconds == 720
            active.timer.function()
            assert core.pod.metadata.finalizers == []
            assert active.receipt["hold_expired"] is True
    assert len(core.patches) == 2


def test_preserves_other_finalizers_and_refuses_replacement_uid():
    core = Core()
    with guard(core):
        core.pod.metadata.finalizers.append("other.example/finalizer")
    assert core.pod.metadata.finalizers == ["other.example/finalizer"]
    core = Core()
    with pytest.raises(ValueError, match="identity changed"):
        with guard(core) as active:
            core.pod.metadata.uid = "replacement"
            active.release()
    assert len(core.patches) == 1


def test_ambiguous_add_response_still_removes_its_own_finalizer():
    core = Core()
    core.fail_add = True
    with pytest.raises(TimeoutError):
        guard(core).__enter__()
    assert core.pod.metadata.finalizers == []
    assert json.loads(core.doc["data"]["receipt-blocked_deletion"])["finalizer_removed"] is True


def test_existing_command_refused_before_finalizer_mutation():
    core = Core()
    core.doc["data"]["command-blocked_deletion"] = json.dumps({"wrong": "run"})
    with pytest.raises(ValueError, match="fresh"):
        guard(core).__enter__()
    assert not core.patches


def test_ambiguous_unapplied_add_is_fenced_before_final_cleanup():
    core = Core()
    original = core.patch_namespaced_pod
    first = True

    def patch(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            raise TimeoutError()
        return original(*args, **kwargs)

    core.patch_namespaced_pod = patch
    with pytest.raises(TimeoutError):
        guard(core).__enter__()
    assert core.pod.metadata.finalizers == []
    assert len(core.patches) == 2  # own add changes RV, then own removal; old CAS is stale
    receipt = Control(core).finalizer_receipt("pod-uid")
    assert receipt["operator_errors"] == ["TimeoutError"]
    assert receipt["finalizer_removed"] is True
