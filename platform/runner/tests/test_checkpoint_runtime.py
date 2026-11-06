import copy

import pytest

from runner.conformance_checkpoint_runtime import gate_ready, pinned_workers
from tests.test_checkpoint_evidence import evidence


def stages_at_gate(case, data):
    return [s for s in data["stages"] if s["generation"] == 1 and not (s["boundary"] == 2)]


@pytest.mark.parametrize("case", ["a", "b", "c", "d", "e"])
def test_gate_needs_real_inflight_history_and_observed_read_completion(case):
    data = evidence(case)
    stages = stages_at_gate(case, data)
    reads = [r for r in data["reads"] if r["generation"] == 1]
    assert gate_ready(case, data["gate_history"], stages, reads, data["receipt"])
    assert not gate_ready(case, [], stages, reads, data["receipt"])
    if case != "a":
        assert not gate_ready(case, data["gate_history"], stages, [], data["receipt"])
    if case == "b":
        assert not gate_ready(
            case,
            data["gate_history"],
            stages + [dict(stages[-1], stage="done")],
            reads,
            data["receipt"],
        )


def test_upload_gate_never_authorizes_after_a_successful_snapshot():
    data = evidence("d")
    history = copy.deepcopy(data["gate_history"])
    history[-1]["completed"] = 99
    history[-1]["result"] = "f" * 64
    assert not gate_ready(
        "d", history, stages_at_gate("d", data), data["reads"][:1], data["receipt"]
    )


def test_worker_images_must_match_production_and_exactly_one_test_worker():
    production = {
        "test_worker": False,
        "image": "registry/runner@sha256:abc",
        "image_id": "registry/runner@sha256:abc",
        "ready": True,
        "restart_count": 0,
    }
    test = dict(production, test_worker=True)
    assert pinned_workers([production, test])
    assert pinned_workers([dict(production, restart_count=1), test])
    assert not pinned_workers([test])
    assert not pinned_workers([production, test, test])
    assert not pinned_workers([production, dict(test, image_id="other")])
    assert not pinned_workers([production, dict(test, ready=False)])


def test_cli_checkpoint_faults_are_exclusive_explicit_and_fixture_bound():
    from runner.conformance import parse_args

    base = [
        "--release",
        "primary",
        "--tests",
        "T23",
        "--checkpoint-fixture-release",
        "signed",
        "--allow-checkpoint-faults",
        "--session-timeout",
        "300",
    ]
    args = parse_args(base)
    assert args.tests == ["T23"] and args.checkpoint_fixture_release == "signed"
    for changed in [
        base[:6] + base[7:],
        base + ["--tenant", "tenant-prod"],
        base + ["--tests", "T23,T4"],
        base + ["--session-timeout", "299"],
    ]:
        with pytest.raises(SystemExit):
            parse_args(changed)


async def test_incomplete_gate_keeps_observed_evidence_without_authorizing_kill():
    from types import SimpleNamespace
    from runner.conformance_checkpoint_runtime import CheckpointHarness
    from tests.test_checkpoint_control import config

    sample = evidence("a")
    gate = dict(sample["receipt"], gate_id="exact-gate")
    ops = SimpleNamespace(
        pods=lambda _: [{"metadata": {"uid": gate["pod_uid"], "name": gate["pod"]}}]
    )
    harness = CheckpointHarness(ops, None, None, "signed", "/artifacts")
    harness.cfg = config()
    harness.control = SimpleNamespace(
        get=lambda _: gate, patch=lambda *args: pytest.fail("must not authorize")
    )

    async def history(_):
        return sample["gate_history"]

    async def logs(_):
        return {
            "logs_readable": False,
            "read_coverage": {"complete": False},
            "reads": [],
            "stages": [],
        }

    harness.history, harness.logs = history, logs
    data = {"session_id": sample["session_id"]}
    assert await harness.release_gate("a", data, None) is False
    assert data["gate"] == gate and data["gate_history"] == sample["gate_history"]
    assert data["gate_observation"]["logs_readable"] is False
