import copy
from types import SimpleNamespace

import pytest
from kubernetes.client.exceptions import ApiException
from temporalio.testing import ActivityEnvironment

from runner.conformance_checkpoint_worker import Hooks
from runner.conformance_checkpoint_control import ConfigConflict
from runner.types import BoundaryIn, SessionInput
from tests.test_checkpoint_control import config


def bound(case="a"):
    cfg = config()
    return BoundaryIn(
        SessionInput(
            cfg["tenant"],
            cfg["agent"],
            cfg["release"],
            cfg["sessions"][case],
            None,
            {"checkpoint_case": case, "run_id": cfg["run_id"]},
            max_boundaries=3,
        ),
        1,
        1,
    )


class Control:
    def __init__(self):
        self.config = config()
        self.values = {}
        self.on_gate = lambda: None

    def get(self, key):
        return copy.deepcopy(self.values.get(key))

    def patch(self, key, expected, value):
        assert self.get(key) == expected
        self.values[key] = copy.deepcopy(value)
        if key == "gate":
            self.on_gate()
            self.values["command"] = {
                "run_id": self.config["run_id"],
                "gate_id": value["gate_id"],
                "action": "kill",
            }


class Core:
    connect_get_namespaced_pod_exec = None

    def __init__(self, inp):
        self.pod = SimpleNamespace(
            metadata=SimpleNamespace(
                uid="pod-original",
                deletion_timestamp=None,
                labels={
                    "agentplatform.io/agent": inp.agent,
                    "agentplatform.io/release": inp.release,
                    "agentplatform.io/session": inp.session_id,
                    "agentplatform.io/generation": "1",
                    "agentplatform.io/tenant": inp.tenant,
                    "agentplatform.io/sandbox": "true",
                },
            ),
            status=SimpleNamespace(phase="Running"),
        )
        self.deletions = []

    def read_namespaced_pod(self, name, namespace, **kwargs):
        if self.pod is None:
            raise ApiException(status=404)
        return self.pod

    def delete_namespaced_pod(self, name, namespace, body, **kwargs):
        self.deletions.append((name, namespace, body))
        assert body.preconditions.uid == self.pod.metadata.uid
        self.pod = None


def test_gate_deletes_only_bound_uid_and_confirms_absence():
    b = bound()
    control, core = Control(), Core(b.inp)
    hooks = Hooks(SimpleNamespace(core=core), control)
    receipt = ActivityEnvironment().run(hooks.gate, "before_exec", b)
    assert receipt["deleted"] and receipt["pod_uid"] == "pod-original"
    assert len(core.deletions) == 1
    assert core.deletions[0][1] == "tenant-test"
    assert control.get("receipt-a") == receipt


def test_pod_replacement_between_gate_and_command_is_never_deleted():
    b = bound()
    control, core = Control(), Core(b.inp)
    control.on_gate = lambda: setattr(core.pod.metadata, "uid", "replacement")
    hooks = Hooks(SimpleNamespace(core=core), control)
    with pytest.raises(ConfigConflict):
        ActivityEnvironment().run(hooks.gate, "before_exec", b)
    assert core.deletions == [] and control.get("receipt-a") is None


def test_worker_refuses_unbound_session_before_any_production_activity():
    b = bound()
    b.inp.session_id = "00000000-0000-4000-8000-999999999999"
    hooks = Hooks(SimpleNamespace(core=None), Control())
    with pytest.raises(ConfigConflict):
        hooks.binding(b.inp)


def test_gate_stage_is_exact_to_fault_case():
    b = bound("d")
    hooks = Hooks(SimpleNamespace(core=None), Control())
    with pytest.raises(ConfigConflict):
        ActivityEnvironment().run(hooks.gate, "before_exec", b)


def test_upload_hook_kills_during_real_copy_and_cannot_commit(tmp_path, monkeypatch):
    from runner import activities as act
    from runner.types import SnapshotIn
    from temporalio.exceptions import ApplicationError
    from tests.fakes import FakeStream, FakeWS
    from tests.test_activities import workspace_tar

    b = bound("d")
    core, control = Core(b.inp), Control()
    blob = workspace_tar() + b"\0" * (2 << 20)
    monkeypatch.setattr(act, "_stream_nocapture", FakeStream(FakeWS([blob], rc=0)))
    deps = act.Deps(
        core=core,
        net=None,
        custom=None,
        aps=None,
        store=act.FilesystemStore(tmp_path / "artifacts"),
    )
    hooks = Hooks(deps, control)
    hooks.install()
    with pytest.raises(ApplicationError) as error:
        ActivityEnvironment().run(hooks.snapshot_workspace, SnapshotIn(b.inp, 1, 1))
    assert error.value.type == "SnapshotFailed"
    receipt = control.get("receipt-d")
    assert receipt["upload"]["copied_bytes"] == act.STDIN_CHUNK
    assert receipt["upload"]["copied_bytes"] < len(blob)
    with deps.store.open(receipt["upload"]["candidate_key"]) as stored:
        assert stored.read() == blob
    assert len(core.deletions) == 1 and core.pod is None


def test_arbitrary_stderr_fields_are_rejected_without_logging_contents(caplog):
    b = bound("b")
    hooks = Hooks(SimpleNamespace(core=None), Control())
    event = {
        "run_id": config()["run_id"],
        "session_id": b.inp.session_id,
        "generation": 1,
        "boundary": 1,
        "case": "b",
        "stage": "read_complete",
        "time": "2026-11-06T18:00:00Z",
        "bearer": "never-log-this-token",
    }
    with pytest.raises(ConfigConflict):
        hooks.stage(b, event)
    assert "never-log-this-token" not in caplog.text
