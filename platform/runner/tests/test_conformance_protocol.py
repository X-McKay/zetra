"""T25 verdicts use trusted history, kernel memory and pod lifecycle evidence."""

import copy
import time

import pytest

from runner import conformance_protocol as p

SNAPSHOT = "a" * 64


def evidence(case="invalid_json"):
    history = []

    def event(activity, generation=1, boundary=None, result=None, failure=None, snapshot=None):
        row = {
            "activity": activity,
            "generation": generation,
            "boundary": boundary,
            "snapshot": snapshot,
            "scheduled": len(history) * 3 + 1,
            "completed": len(history) * 3 + 3,
            "result": result,
            "failure_type": "OutcomeUnknown" if failure else None,
            "failure_message": failure,
        }
        history.append(row)
        return row

    event("set_generation")
    event("create_sandbox")
    event("run_boundary", boundary=0, result={"boundary": 0, "done": case == "control"})
    event("snapshot_workspace", boundary=0, result=SNAPSHOT)
    for generation in [] if case == "control" else [1, 2, 3]:
        if generation > 1:
            event("set_generation", generation)
            event("create_sandbox", generation, snapshot=SNAPSHOT)
        event(
            "run_boundary",
            generation,
            1,
            failure={
                "stdout_10mib": "rc=None truncated=True timed_out=False protocol_error=unrecognized",
                "invalid_json": "malformed boundary output",
                "wrong_boundary": "inconsistent boundary output",
                "done_true_rc0": "inconsistent boundary output",
                "hang": "rc=None truncated=False timed_out=True protocol_error=unrecognized",
            }[case],
        )
        event("delete_sandbox", generation, result="DELETED")
    if case == "control":
        event("delete_sandbox", result="DELETED")
    event("stop_session", generation=None, result="OK")
    return {
        "session_id": "session-" + case,
        "case": case,
        "outcome": "COMPLETED" if case == "control" else "FAILED",
        "generation": 1 if case == "control" else 3,
        "boundaries": 1,
        "snapshot": SNAPSHOT,
        "cleanup": {"stop": "OK", "grant": "OK", "delete": "DELETED"},
        "status": {"state": "CLEANED"},
        "history": history,
        "pods": [],
    }


@pytest.mark.parametrize("case", ["control", *p.FAULTS])
def test_exact_recovery_and_healthy_control(case):
    assert p.evaluate_session(case, evidence(case))["result"] == "pass"


@pytest.mark.parametrize(
    "mutation",
    [
        "accepted_bad",
        "missing_failure",
        "wrong_failure",
        "unconfirmed",
        "create_before_delete",
        "wrong_snapshot",
        "extra_checkpoint",
        "pods_remain",
        "wrong_generation",
        "stop_failed",
    ],
)
def test_missing_or_wrong_recovery_evidence_never_passes(mutation):
    data = evidence()
    events = data["history"]
    failed = next(e for e in events if e["activity"] == "run_boundary" and e["boundary"] == 1)
    deleted = next(e for e in events if e["activity"] == "delete_sandbox")
    created = next(e for e in events if e["activity"] == "create_sandbox" and e["generation"] == 2)
    checkpoint = next(e for e in events if e["activity"] == "snapshot_workspace")
    if mutation == "accepted_bad":
        failed["failure_type"] = None
        failed["result"] = {"boundary": 1, "done": True}
    elif mutation == "missing_failure":
        events.remove(failed)
    elif mutation == "wrong_failure":
        failed["failure_type"] = "Forbidden"
    elif mutation == "unconfirmed":
        deleted["result"] = "NOT_CONFIRMED"
    elif mutation == "create_before_delete":
        created["scheduled"] = deleted["completed"] - 1
    elif mutation == "wrong_snapshot":
        created["snapshot"] = "b" * 64
    elif mutation == "extra_checkpoint":
        events.append(copy.deepcopy(checkpoint))
    elif mutation == "pods_remain":
        data["pods"] = ["sandbox"]
    elif mutation == "wrong_generation":
        data["generation"] = 2
    else:
        data["cleanup"]["stop"] = "FAILED"
    assert p.evaluate_session("invalid_json", data)["result"] == "fail"


def test_fence_and_delete_events_must_prove_the_real_order():
    data = evidence()
    fences = [e for e in data["history"] if e["activity"] == "set_generation"]
    fences[1]["scheduled"] = 1
    assert p.evaluate_session("invalid_json", data)["result"] == "fail"


def samples(data):
    result = []
    for event in data["history"]:
        if event["activity"] != "run_boundary":
            continue
        for phase in ("start", "end"):
            result.append(
                {
                    "event": "boundary_memory",
                    "session_id": data["session_id"],
                    "generation": event["generation"],
                    "boundary": event["boundary"],
                    "phase": phase,
                    "sampled_at": time.time() + len(result) / 1000,
                    "elapsed_ms": (
                        290000 if data["case"] == "hang" and event["boundary"] == 1 else 1
                    )
                    if phase == "end"
                    else 0,
                    "process_id": "worker:1:1234",
                    "rss_bytes": 100 << 20,
                    "hwm_bytes": 110 << 20,
                    "limit_bytes": 1 << 30,
                    "error": None,
                }
            )
    return result


def worker():
    return [
        {
            "name": "worker",
            "uid": "uid1",
            "restart_count": 0,
            "ready": True,
            "image_id": "repo@sha256:" + "c" * 64,
        }
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "missing",
        "missing_end",
        "rss_growth",
        "hwm_growth",
        "near_limit",
        "restart",
        "new_uid",
        "new_process",
        "unknown_limit",
        "unreadable",
        "concurrent_session",
        "not_ready",
    ],
)
def test_memory_requires_complete_same_process_bounded_samples_and_stable_worker(mutation):
    data = evidence()
    records = samples(data)
    inventories = [worker(), worker()]
    if mutation == "missing":
        records = []
    elif mutation == "missing_end":
        records.pop()
    elif mutation == "rss_growth":
        records[-1].update(rss_bytes=200 << 20, hwm_bytes=200 << 20)
    elif mutation == "hwm_growth":
        records[-1]["hwm_bytes"] = 200 << 20
    elif mutation == "near_limit":
        records[-1]["hwm_bytes"] = (1 << 30) - 1
    elif mutation == "restart":
        inventories[-1][0]["restart_count"] = 1
    elif mutation == "new_uid":
        inventories[-1][0]["uid"] = "uid2"
    elif mutation == "new_process":
        records[-1]["process_id"] = "worker:1:2345"
    elif mutation == "unknown_limit":
        records[-1]["limit_bytes"] = None
    elif mutation == "unreadable":
        records.append({"msg": "log unavailable", "pod": "worker"})
    elif mutation == "concurrent_session":
        records.append({**records[0], "session_id": "somebody-else"})
    elif mutation == "not_ready":
        inventories[-1][0]["ready"] = False
    result = p.evaluate_memory([data], records, inventories)
    assert result["result"] == ("pass" if mutation is None else "fail")


def test_hang_requires_the_real_deadline_not_an_injected_short_timeout():
    data = evidence("hang")
    records = samples(data)
    assert p.evaluate_memory([data], records, [worker(), worker()])["result"] == "pass"
    records[-1]["elapsed_ms"] = 200
    assert p.evaluate_memory([data], records, [worker(), worker()])["result"] == "fail"


def test_cli_requires_explicit_protocol_fixture_and_long_timeout():
    from runner.conformance import parse_args

    args = ["--release", "main-release", "--tests", "T25"]
    with pytest.raises(SystemExit):
        parse_args(args)
    args += [
        "--protocol-fixture-agent",
        "conformance-protocol",
        "--protocol-fixture-release",
        "protocol-release",
    ]
    with pytest.raises(SystemExit):
        parse_args(args)
    selected = parse_args(args + ["--session-timeout", "1800"])
    assert selected.tests == ["T25"]


async def test_real_temporal_history_decodes_restore_fields_and_delete_order():
    from temporalio import activity
    from temporalio.exceptions import ApplicationError
    from temporalio.worker import Worker
    from tests.harness import FakeActivities, _environment
    from runner.types import SnapshotIn
    from runner.workflow import SessionWorkflow
    from types import SimpleNamespace

    class Activities(FakeActivities):
        @activity.defn(name="snapshot_workspace")
        async def snapshot_workspace(self, value: SnapshotIn) -> str:
            return SNAPSHOT

    def fault(generation, boundary):
        if boundary == 1:
            raise ApplicationError("malformed boundary output", type="OutcomeUnknown")

    fake = Activities(boundary_plan=fault)
    env = await _environment()
    async with env:
        async with Worker(
            env.client,
            task_queue="sessions-tenant-test",
            workflows=[SessionWorkflow],
            activities=fake.all(),
        ):
            ops = SimpleNamespace(tenant="tenant-test", poll=0.001, pods=lambda session: [])
            driver = p.ProtocolSessions(
                env.client, ops, "conformance-protocol", "fixture", timeout=1800
            )
            data = await driver.run("invalid_json")
            out = p.evaluate_session("invalid_json", data)
            assert out["result"] == "pass", out
            assert fake.restores == {1: None, 2: SNAPSHOT, 3: SNAPSHOT}
            assert all(e["scheduled"] < e["completed"] for e in data["history"])


async def test_harness_runs_a_healthy_session_after_each_fault_and_requires_ready_fixture():
    from types import SimpleNamespace

    called, runs = [], []
    rel = {
        "spec": {"agent": "conformance-protocol", "approval": {"approved": True}},
        "status": {"ready": True, "signatureVerified": True, "policyDrift": False},
    }

    async def session(case):
        called.append(case)
        data = evidence(case)
        data["session_id"] += str(len(called))
        runs.append(data)
        return data

    async def logs(since):
        import json

        return "\n".join(json.dumps(s) for data in runs for s in samples(data))

    ops = SimpleNamespace(release=lambda name: rel, workers=worker)
    driver = SimpleNamespace(run=session)
    harness = p.ProtocolHarness(ops, driver, logs, "conformance-protocol", "fixture")
    result = await harness.t25()
    assert called == ["control"] + [x for fault in p.FAULTS for x in (fault, "control")]
    assert len(result["cases"]) == 11
    assert result["result"] == "pass", result
    rel["status"]["ready"] = False
    called.clear()
    assert (await harness.t25())["result"] == "error" and called == []
