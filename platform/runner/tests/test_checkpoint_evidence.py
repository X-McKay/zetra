import copy
import hashlib
import io
import json
import tarfile

import pytest

from runner.conformance_checkpoint import checkpoint_state, evaluate_case
from runner.activities import pod_name
from tests.test_checkpoint_control import config


def evidence(case):
    cfg = config()
    session = cfg["sessions"][case]
    history, stages, states, reads = [], [], {}, []
    gate_history = []
    event = 0

    def activity(name, generation, boundary=None, result="OK", snapshot=None, failed=None):
        nonlocal event
        event += 2
        row = {
            "activity": name,
            "generation": generation,
            "boundary": boundary,
            "scheduled": event,
            "completed": event + 1,
            "result": result,
            "snapshot": snapshot,
            "failure_type": failed,
        }
        history.append(row)
        return row

    def provision(generation, restore=None):
        for name in ["set_generation", "create_sandbox", "wait_ready"]:
            activity(name, generation, snapshot=restore)

    def stage(name, generation, boundary):
        stages.append(
            {
                "stage": name,
                "generation": generation,
                "boundary": boundary,
                "session_id": session,
                "run_id": cfg["run_id"],
                "case": "control" if case.startswith("control-") else case,
            }
        )

    def run(generation, boundary, failure=None):
        row = activity(
            "run_boundary",
            generation,
            boundary,
            result=None if failure else {"boundary": boundary, "done": boundary == 2},
            failed=failure,
        )
        if not failure or case == "b":
            stage("boundary_start", generation, boundary)
            if boundary == 1:
                stage("read_complete", generation, boundary)
                reads.append(
                    {
                        "generation": generation,
                        "decision_id": f"read-{generation}",
                        "pod": pod_name(session, generation),
                        "tool": "pods_list_in_namespace",
                        "reason": "allow",
                        "gateway_count": 1,
                        "capture_count": 1,
                        "gateway_status": "200",
                        "capture_status": 200,
                        "capture_bound": True,
                    }
                )
            if not failure:
                stage("done", generation, boundary)
        return row

    def snap(generation, boundary, failure=False):
        key = f"{generation}{boundary}" * 32
        row = activity(
            "snapshot_workspace",
            generation,
            boundary,
            result=None if failure else key,
            failed="SnapshotFailed" if failure else None,
        )
        if not failure:
            states[key] = {
                "marker": {
                    "run_id": cfg["run_id"],
                    "session_id": session,
                    "last_boundary": boundary,
                    "generation": generation,
                },
                "next": boundary + 1,
                "done": True,
                "bytes": 2 << 20,
            }
        return row, key

    provision(1)
    run(1, 0)
    _, baseline = snap(1, 0)
    if case.startswith("control-"):
        run(1, 1)
        snap(1, 1)
        run(1, 2)
        _, final = snap(1, 2)
        generation = 1
    else:
        if case in ("a", "b"):
            run(1, 1, "SandboxLost" if case == "a" else "OutcomeUnknown")
            restore = baseline
        elif case in ("c", "d"):
            run(1, 1)
            snap(1, 1, True)
            restore = baseline
        else:
            run(1, 1)
            _, restore = snap(1, 1)
            run(1, 2, "SandboxLost")
        gate_history = copy.deepcopy(history)
        gate_history[-1].update(completed=None, result=None, failure_type=None)
        activity("delete_sandbox", 1, result="DELETED")
        provision(2, restore)
        if case != "e":
            run(2, 1)
            snap(2, 1)
        run(2, 2)
        _, final = snap(2, 2)
        generation = 2
    activity("delete_sandbox", generation, result="DELETED")
    points = {
        "a": "before_exec",
        "b": "after_read_before_done",
        "c": "before_snapshot",
        "d": "during_store_copy",
        "e": "after_commit",
    }
    receipt = {
        "run_id": cfg["run_id"],
        "case": case,
        "session_id": session,
        "generation": 1,
        "boundary": 2 if case == "e" else 1,
        "stage": points.get(case),
        "deleted": True,
        "pod": pod_name(session, 1),
        "pod_uid": "real-uid",
        "reached_at": "2026-11-06T18:00:00Z",
        "delete_started_at": "2026-11-06T18:00:01Z",
        "deleted_at": "2026-11-06T18:00:02Z",
    }
    if case == "d":
        receipt["upload"] = {"copied_bytes": 1 << 20, "candidate_key": "11" * 32}
    candidate = (
        {
            "marker": {
                "run_id": cfg["run_id"],
                "session_id": session,
                "last_boundary": 1,
                "generation": 1,
            },
            "next": 2,
            "done": True,
            "bytes": 3 << 20,
        }
        if case == "d"
        else None
    )
    return {
        "run_id": cfg["run_id"],
        "session_id": session,
        "outcome": "COMPLETED",
        "boundaries": 3,
        "generation": generation,
        "snapshot": final,
        "pods": [],
        "cleanup": {"stop": "OK", "delete": "DELETED", "grant": "OK"},
        "history": history,
        "gate_history": gate_history,
        "stages": stages,
        "checkpoints": states,
        "reads": reads,
        "receipt": receipt,
        "logs_readable": True,
        "read_coverage": {"complete": True},
        "candidate": candidate,
    }


@pytest.mark.parametrize("case", list(config()["sessions"]))
def test_all_five_failure_points_and_healthy_controls_require_complete_evidence(case):
    result = evaluate_case(case, evidence(case))
    assert result["result"] == "pass", result["checks"]


@pytest.mark.parametrize(
    "fault",
    [
        "missing_read",
        "duplicate_read",
        "missing_capture",
        "wrong_status",
        "no_cleanup",
        "wrong_restore",
        "delete_after_create",
        "wrong_checkpoint",
        "missing_receipt",
        "wrong_stage_identity",
        "missing_done_stage",
    ],
)
def test_missing_or_misordered_evidence_never_passes(fault):
    data = evidence("c")
    if fault == "missing_read":
        data["reads"].pop()
    elif fault == "duplicate_read":
        data["reads"].append(data["reads"][0])
    elif fault == "missing_capture":
        data["reads"][0]["capture_count"] = 0
    elif fault == "wrong_status":
        data["reads"][0]["gateway_status"] = 403
    elif fault == "no_cleanup":
        data["pods"] = ["remaining-pod"]
    elif fault == "wrong_restore":
        next(
            h for h in data["history"] if h["activity"] == "create_sandbox" and h["generation"] == 2
        )["snapshot"] = "f" * 64
    elif fault == "delete_after_create":
        next(
            h for h in data["history"] if h["activity"] == "delete_sandbox" and h["generation"] == 1
        )["completed"] = 10000
    elif fault == "wrong_checkpoint":
        next(iter(data["checkpoints"].values()))["marker"]["last_boundary"] = 99
    elif fault == "missing_receipt":
        data["receipt"] = None
    elif fault == "wrong_stage_identity":
        data["stages"][0]["session_id"] = "other"
    else:
        data["stages"] = [s for s in data["stages"] if s["stage"] != "done"]
    assert evaluate_case("c", data)["result"] == "fail"


def test_checkpoint_reader_verifies_hash_and_never_extracts_untrusted_paths(tmp_path):
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w") as archive:
        for name, value in [
            ("workspace/checkpoint.json", {"last_boundary": 1}),
            ("state/state.json", {"next": 2}),
            ("state/1.done", 1),
        ]:
            raw = json.dumps(value).encode()
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    key = hashlib.sha256(blob.getvalue()).hexdigest()
    path = tmp_path / key[:2] / key
    path.parent.mkdir()
    path.write_bytes(blob.getvalue())
    assert checkpoint_state(tmp_path, key)["next"] == 2
    path.write_bytes(blob.getvalue() + b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        checkpoint_state(tmp_path, key)


@pytest.mark.parametrize("fault", ["missing", "after_copy", "accepted_candidate"])
def test_upload_gate_requires_real_partial_copy_and_unaccepted_candidate(fault):
    data = evidence("d")
    if fault == "missing":
        data["receipt"].pop("upload")
    elif fault == "after_copy":
        data["receipt"]["upload"]["copied_bytes"] = data["candidate"]["bytes"]
    else:
        data["receipt"]["upload"]["candidate_key"] = data["snapshot"]
    assert evaluate_case("d", data)["result"] == "fail"


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "extra_capture",
        "extra_gateway",
        "missing_aps",
        "duplicate_aps",
        "duplicate_gateway",
        "duplicate_capture",
        "empty_id",
    ],
)
def test_transport_join_requires_complete_unique_selected_session_ids(fault):
    from runner.conformance_checkpoint import read_evidence

    session = config()["sessions"]["b"]
    aps = [
        {
            "session_id": session,
            "route": "mcp",
            "method": method,
            "backend": "kubernetes",
            "decision_id": key,
            "reason": "allow",
            "tool": "pods_list_in_namespace" if method == "tools/call" else None,
            "generation": 1,
            "pod": pod_name(session, 1),
        }
        for key, method in [("init", "initialize"), ("read", "tools/call")]
    ]
    gateway = [
        {
            "agent.session_id": session,
            "agent.decision_id": row["decision_id"],
            "http.path": "/kubernetes/mcp",
            "http.status": "200",
        }
        for row in aps
    ]
    capture = [
        {
            "x_agent": {
                "x-agent-session-id": session,
                "x-agent-decision-id": row["decision_id"],
                "x-agent-id": "tenant-test/conformance-checkpoint",
                "x-agent-generation": "1",
            },
            "method": "POST",
            "path": "/mcp",
            "rpc_method": rpc,
            "status": 202 if rpc == "notifications/initialized" else 200,
        }
        for row in aps
        for rpc in (
            ["initialize"]
            if row["method"] == "initialize"
            else ["initialize", "notifications/initialized", row["method"]]
        )
    ]
    if fault == "extra_capture":
        capture.append(
            {
                "x_agent": dict(capture[-1]["x_agent"], **{"x-agent-decision-id": "orphan"}),
                "status": 200,
            }
        )
    elif fault == "extra_gateway":
        gateway.append(dict(gateway[-1], **{"agent.decision_id": "orphan"}))
    elif fault == "missing_aps":
        aps.pop()
    elif fault == "duplicate_aps":
        aps.append(aps[-1])
    elif fault == "duplicate_gateway":
        gateway.append(gateway[-1])
    elif fault == "duplicate_capture":
        capture.append(capture[-1])
    elif fault == "empty_id":
        capture[-1]["x_agent"]["x-agent-decision-id"] = ""
    result = read_evidence(session, aps, gateway, capture)
    assert result["read_coverage"]["complete"] is (fault is None)
    if fault is None:
        assert len(result["reads"]) == 1  # initialize remains correlated, never counted as a read


@pytest.mark.parametrize("case", ["a", "b", "c", "d", "e"])
async def test_evaluator_accepts_real_decoded_session_workflow_history(case):
    """Real Temporal event names/order, with unit-only activity failures and external evidence.

    This validates the adapter contract, not live Kubernetes/MCP qualification.
    """
    import asyncio
    from dataclasses import asdict
    from temporalio import activity
    from temporalio.exceptions import ApplicationError
    from temporalio.worker import Worker
    from runner.conformance_protocol import decode_history
    from runner.types import BoundaryIn, SnapshotIn
    from runner.workflow import SessionWorkflow
    from tests.harness import FakeActivities, _environment
    from tests.test_checkpoint_worker import bound

    reached, release = asyncio.Event(), asyncio.Event()

    async def pause():
        reached.set()
        await release.wait()

    class Activities(FakeActivities):
        @activity.defn(name="run_boundary")
        async def run_boundary(self, b: BoundaryIn):
            if b.generation == 1 and (
                (case in ("a", "b") and b.boundary == 1) or (case == "e" and b.boundary == 2)
            ):
                await pause()
                raise ApplicationError(
                    "unit boundary failure", type="SandboxLost", non_retryable=True
                )
            return await super().run_boundary(b)

        @activity.defn(name="snapshot_workspace")
        async def snapshot_workspace(self, s: SnapshotIn) -> str:
            if case in ("c", "d") and (s.generation, s.boundary) == (1, 1):
                await pause()
                raise ApplicationError(
                    "unit upload failure", type="SnapshotFailed", non_retryable=True
                )
            return f"{s.generation}{s.boundary}" * 32

    inp = bound(case).inp
    env = await _environment()
    async with env:
        fake = Activities(done_at=2)
        async with Worker(
            env.client,
            task_queue=inp.task_queue,
            workflows=[SessionWorkflow],
            activities=fake.all(),
        ):
            handle = await env.client.start_workflow(
                SessionWorkflow.run, inp, id=inp.workflow_id, task_queue=inp.task_queue
            )
            await asyncio.wait_for(reached.wait(), 20)
            before = await handle.fetch_history()
            gate_history = await decode_history(before.events, env.client.data_converter)
            release.set()
            result = await handle.result()
            after = await handle.fetch_history()
            actual = await decode_history(after.events, env.client.data_converter)
    data = evidence(case)
    data.update(asdict(result), history=actual, gate_history=gate_history)
    assert "set_generation" in {h["activity"] for h in actual}
    verdict = evaluate_case(case, data)
    assert verdict["result"] == "pass", verdict["checks"]
