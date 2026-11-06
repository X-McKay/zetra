import copy
import uuid
from types import SimpleNamespace as NS

import pytest

from runner.conformance_generation import evaluate_case
from runner.conformance_generation_control import FINALIZER, Control, validate_config


def config():
    run = str(uuid.uuid4())
    return {
        "version": 1,
        "run_id": run,
        "tenant": "tenant-test",
        "probe_release": "conformance-b-test",
        "recovery_release": "conformance-checkpoint-test",
        "queue": "conformance-generation-" + run,
        "worker_image": "registry.almckay.io/platform/session-runner:test@sha256:" + "a" * 64,
        "sessions": {
            c: str(uuid.uuid4()) for c in ("fence_only", "blocked_deletion", "recovery_control")
        },
    }


def data(case):
    d = {
        "session_id": "session",
        "initial_pod": {
            "name": "g1",
            "uid": "uid1",
            "generation": 1,
            "phase": "Running",
            "deleting": False,
        },
        "worker_before": {"uid": "worker"},
        "worker_after": {"uid": "worker"},
        "cleanup": {"session_closed": True, "pods_absent": True, "finalizer_absent": True},
        "history": [],
        "samples": [],
    }
    if case == "fence_only":
        d.update(
            aps_replicas=["aps1", "aps2"],
            worker_receipts=[
                {
                    "operation": "fence_generation_2",
                    "session_id": "session",
                    "pod_uid": "uid1",
                    "generation": 2,
                    "acknowledged": True,
                }
            ],
            allowed_requests=[
                {"source_pod": p, "status": 200, "reason": "allow", "token_jti": "fixed"}
                for p in ("aps1", "aps2")
            ],
            stale_requests=[
                {
                    "source_pod": p,
                    "status": 403,
                    "reason": "deny.generation_stale",
                    "token_jti": "fixed",
                    "generation": 1,
                }
                for p in ("aps1", "aps2")
            ],
        )
        d["samples"] = [{"phase": "stale_denial", "pods": [d["initial_pod"]]}]
    elif case == "blocked_deletion":
        d.update(
            outcome="NOT_CONFIRMED",
            worker_receipts=[
                {
                    "operation": "delete_for_recovery",
                    "pod_uid": "uid1",
                    "observed": "terminated_and_held",
                    "acknowledged": True,
                }
            ],
            finalizer_receipt={
                "target_uid": "uid1",
                "finalizer_added": True,
                "finalizer_removed": True,
                "hold_expired": False,
                "operator_errors": [],
            },
            reconciler_after_removal_seconds=2,
            harness_monotonic_bound=True,
            scheduled_gate_before_removal=True,
            reconciler=[
                {
                    "activity": "delete_orphan",
                    "result": "DELETED",
                    "scheduled_action": True,
                    "pod_name": "g1",
                }
            ],
        )
        d["samples"] = [
            {"pods": [{**d["initial_pod"], "deleting": True, "finalizers": [FINALIZER]}]}
        ]
        d["history"] = [
            {
                "activity": "delete_sandbox",
                "generation": 1,
                "result": "NOT_CONFIRMED",
                "duration": 110,
                "scheduled_id": 9,
                "completed_id": 10,
            }
        ]
    else:
        d.update(
            outcome="COMPLETED",
            generation=2,
            boundaries=3,
            worker_receipts=[
                {"operation": "delete_for_recovery", "pod_uid": "uid1", "observed": "404"}
            ],
        )
        d["history"] = [
            {
                "activity": "delete_sandbox",
                "generation": 1,
                "result": "DELETED",
                "completed_id": 10,
            },
            {"activity": "create_sandbox", "generation": 2, "scheduled_id": 11, "result": "uid2"},
            {"activity": "run_boundary", "generation": 2, "result": {"done": True}},
        ]
    if case != "fence_only":
        d["history"].insert(
            0,
            {
                "activity": "run_boundary",
                "generation": 1,
                "failure_type": "SandboxLost",
                "completed_id": 8,
            },
        )
        for row in d["history"]:
            if row.get("activity") == "delete_sandbox":
                row.setdefault("scheduled_id", 9)
    return d


@pytest.mark.parametrize("case", ["fence_only", "blocked_deletion", "recovery_control"])
def test_three_separate_complete_cases_pass(case):
    assert evaluate_case(case, data(case))["result"] == "pass"


def test_blocked_delete_rejects_duration_below_production_deadline():
    d = data("blocked_deletion")
    next(h for h in d["history"] if h.get("activity") == "delete_sandbox")["duration"] = 109
    assert not evaluate_case("blocked_deletion", d)["checks"][
        "ordinary_recovery_stopped_not_confirmed"
    ]


@pytest.mark.parametrize(
    "case,fault",
    [
        ("fence_only", "different_token"),
        ("fence_only", "wrong_reason"),
        ("fence_only", "g1_not_running"),
        ("fence_only", "g2_created"),
        ("fence_only", "no_ack"),
        ("blocked_deletion", "g2_created"),
        ("blocked_deletion", "fake_timeout"),
        ("blocked_deletion", "late_reconciler"),
        ("blocked_deletion", "wrong_clock"),
        ("blocked_deletion", "manual_reconciler"),
        ("blocked_deletion", "expired_root_hold"),
        ("blocked_deletion", "root_operator_error"),
        ("recovery_control", "create_before_delete"),
        ("recovery_control", "no_404"),
        ("recovery_control", "no_completion"),
    ],
)
def test_unsafe_or_incomplete_cases_never_pass(case, fault):
    d = copy.deepcopy(data(case))
    if fault == "different_token":
        d["stale_requests"][0]["token_jti"] = "different"
    elif fault == "wrong_reason":
        d["stale_requests"][0]["reason"] = "deny.session_inactive"
    elif fault == "g1_not_running":
        d["samples"][0]["pods"][0]["deleting"] = True
    elif fault == "g2_created":
        d["history"].append({"activity": "create_sandbox", "generation": 2})
    elif fault == "no_ack":
        d["worker_receipts"] = []
    elif fault == "fake_timeout":
        next(h for h in d["history"] if h.get("activity") == "delete_sandbox")["duration"] = 1
    elif fault == "late_reconciler":
        d["reconciler_after_removal_seconds"] = 300.01
    elif fault == "wrong_clock":
        d["harness_monotonic_bound"] = False
    elif fault == "manual_reconciler":
        d["reconciler"][0]["scheduled_action"] = False
    elif fault == "expired_root_hold":
        d["finalizer_receipt"]["hold_expired"] = True
    elif fault == "root_operator_error":
        d["finalizer_receipt"]["operator_errors"] = ["TimeoutError"]
    elif fault == "create_before_delete":
        next(h for h in d["history"] if h.get("activity") == "create_sandbox")["scheduled_id"] = 9
    elif fault == "no_404":
        d["worker_receipts"] = []
    elif fault == "no_completion":
        d["outcome"] = "FAILED"
    assert evaluate_case(case, d)["result"] == "fail"


def test_run_config_and_root_command_require_exact_fresh_binding():
    import json

    cfg = config()
    doc = {"metadata": {"uid": "control"}, "data": {"config": json.dumps(cfg)}}
    control = Control(NS(read_namespaced_config_map=lambda *a, **kw: doc))
    sid = cfg["sessions"]["fence_only"]
    command = {
        "run_id": cfg["run_id"],
        "session_id": sid,
        "pod_uid": "pod",
        "worker_uid": "worker",
        "action": "fence_generation_2",
    }
    doc["data"]["command-fence_only"] = json.dumps(command)
    assert control.commanded("fence_only", "pod", "worker")
    for field in command:
        changed = dict(command, **{field: "wrong"})
        doc["data"]["command-fence_only"] = json.dumps(changed)
        with pytest.raises(ValueError):
            control.commanded("fence_only", "pod", "worker")
    doc["metadata"]["uid"] = "replacement"
    with pytest.raises(ValueError):
        control.read()


@pytest.mark.parametrize(
    "fault", ["tenant", "queue", "duplicate_session", "unpinned_image", "unknown_case"]
)
def test_config_does_not_expand_scope(fault):
    cfg = config()
    if fault == "tenant":
        cfg["tenant"] = "tenant-prod"
    elif fault == "queue":
        cfg["queue"] = "sessions-tenant-test"
    elif fault == "duplicate_session":
        cfg["sessions"]["blocked_deletion"] = cfg["sessions"]["fence_only"]
    elif fault == "unpinned_image":
        cfg["worker_image"] = "latest"
    else:
        cfg["sessions"]["other"] = str(uuid.uuid4())
    with pytest.raises(ValueError):
        validate_config(cfg)
