import copy

import pytest

from runner.conformance_lifecycle import evaluate_case


def evidence(case="finalizer"):
    initial = {
        "session_id": "session",
        "tenant": "tenant-test",
        "agent": "conformance-lifecycle",
        "observed_at": 1000.0,
        "generation": 1,
        "generation_present": True,
        "grant_present": True,
        "vault_present": True,
        "grant_ttl_seconds": 3600,
        "vault_ttl_seconds": 3600,
        "refresh_backed": True,
        "access_current": True,
        "stopped": False,
        "cleanup_pending": False,
    }
    final = {
        **initial,
        "observed_at": 1220.0,
        "generation": 0,
        "generation_present": False,
        "grant_present": False,
        "vault_present": False,
        "stopped": True,
        "cleanup_pending": False,
    }
    history = [
        {
            "activity": "run_boundary",
            "generation": 1,
            "boundary": 0,
            "result": {"done": False},
            "scheduled_at": 990.0,
            "completed_at": 991.0,
        },
        {
            "activity": "snapshot_workspace",
            "generation": 1,
            "boundary": 0,
            "result": "a" * 64,
            "scheduled_at": 992.0,
            "completed_at": 993.0,
        },
        {
            "activity": "stop_session",
            "result": "OK",
            "scheduled_at": 1100.0,
            "completed_at": 1102.0,
        },
        {
            "activity": "delete_grant",
            "result": "OK",
            "scheduled_at": 1100.0,
            "completed_at": 1102.0,
        },
        {
            "activity": "delete_sandbox",
            "result": "NOT_CONFIRMED",
            "scheduled_at": 1100.0,
            "completed_at": 1211.0,
        },
    ]
    sample = {
        "observed_at": 1110.0,
        "observer": {**final, "observed_at": 1110.0},
        "pods": [
            {
                "name": "pod",
                "uid": "pod-uid",
                "generation": 1,
                "deleting": True,
                "finalizers": ["agentplatform.io/conformance-lifecycle"],
            }
        ],
    }
    return {
        "case": case,
        "session_id": "session",
        "initial": initial,
        "final": final,
        "baseline_gate": {
            "stage": "boundary1",
            "session_id": "session",
            "generation": 1,
            "worker_uid": "worker-old",
        },
        "initial_pods": [{"name": "pod", "uid": "pod-uid", "generation": 1}],
        "final_pods": [],
        "pod_absence": [{"name": "pod", "uid": "pod-uid", "http_status": 404}],
        "samples": [sample],
        "history": history,
        "reconciler": [],
        "terminal": {"kind": "COMPLETED", "at": 1212.0},
        "anchor": 1100.0,
        "elapsed_monotonic": 120.0,
        "receipt": {
            "session_id": "session",
            "fault": case,
            "target_uid": "pod-uid",
            "started_at": 1099.0,
            "restored_at": 1215.0,
        },
        "restored": True,
        "evidence_errors": [],
    }


def test_actual_finalizer_cleanup_requires_independent_stop_and_confirmed_absence():
    assert evaluate_case("finalizer", evidence())["result"] == "pass"


@pytest.mark.parametrize(
    "mutation",
    [
        "empty_grant",
        "short_grant",
        "expired_naturally",
        "missing_vault",
        "generation_key",
        "grant_remains",
        "pending",
        "late",
        "negative_time",
        "missing_404",
        "remaining_pod",
        "missing_checkpoint",
        "wrong_session",
        "no_fault",
        "no_independence",
        "false_delete",
        "not_restored",
        "evidence_error",
    ],
)
def test_incomplete_or_false_cleanup_proof_is_rejected(mutation):
    d = evidence()
    if mutation == "empty_grant":
        d["initial"]["grant_present"] = False
    elif mutation == "short_grant":
        d["initial"]["grant_ttl_seconds"] = 600
    elif mutation == "expired_naturally":
        d["final"]["observed_at"] = 5000
    elif mutation == "missing_vault":
        d["initial"]["vault_present"] = False
    elif mutation == "generation_key":
        d["final"]["generation_present"] = True
    elif mutation == "grant_remains":
        d["final"]["grant_present"] = True
    elif mutation == "pending":
        d["final"]["cleanup_pending"] = True
    elif mutation == "late":
        d["elapsed_monotonic"] = 600.001
    elif mutation == "negative_time":
        d["final"]["observed_at"] = 900
    elif mutation == "missing_404":
        d["pod_absence"] = []
    elif mutation == "remaining_pod":
        d["final_pods"] = ["pod"]
    elif mutation == "missing_checkpoint":
        d["history"] = d["history"][1:]
    elif mutation == "wrong_session":
        d["final"]["session_id"] = "other"
    elif mutation == "no_fault":
        d["samples"][0]["pods"][0]["finalizers"] = []
    elif mutation == "no_independence":
        d["samples"][0]["observer"]["grant_present"] = True
    elif mutation == "false_delete":
        d["history"][-1]["result"] = "DELETED"
    elif mutation == "not_restored":
        d["restored"] = False
    else:
        d["evidence_errors"] = ["unreadable history"]
    assert evaluate_case("finalizer", d)["result"] == "fail"


def test_hard_termination_needs_actual_terminal_event_and_scheduled_reconciler_results():
    d = evidence("terminate")
    d["history"] = d["history"][:2]
    d["terminal"] = {"kind": "TERMINATED", "at": 1100.0}
    d["receipt"] = None
    d["reconciler"] = [
        {
            "activity": "close_session",
            "result": "OK",
            "scheduled_at": 1190.0,
            "completed_at": 1200.0,
            "scheduled_action": True,
        },
        {
            "activity": "delete_orphan",
            "result": "DELETED",
            "scheduled_at": 1190.0,
            "completed_at": 1200.0,
            "scheduled_action": True,
        },
    ]
    assert evaluate_case("terminate", d)["result"] == "pass"
    for mutate in ("cancel", "manual", "no_delete"):
        bad = copy.deepcopy(d)
        if mutate == "cancel":
            bad["terminal"]["kind"] = "CANCELED"
        elif mutate == "manual":
            bad["reconciler"][0]["scheduled_action"] = False
        else:
            bad["reconciler"].pop()
        assert evaluate_case("terminate", bad)["result"] == "fail"


def aps_evidence():
    d = evidence("aps_down")
    d["receipt"]["target_uid"] = "aps-uid"
    d["history"][-3]["result"] = None
    d["history"][-3]["failure_type"] = "APSUnavailable"
    d["history"][-3]["completed_at"] = 1140.0
    d["history"][-2]["result"] = None
    d["history"][-2]["failure_type"] = "APSUnavailable"
    d["history"][-2]["completed_at"] = 1141.0
    d["history"][-1]["result"] = "DELETED"
    d["history"][-1]["completed_at"] = 1120.0
    baseline = {
        "uid": "aps-uid",
        "spec_sha": "x",
        "replicas": 2,
        "ready_replicas": 2,
        "pods": [{"uid": "old-a", "ready": True}, {"uid": "old-b", "ready": True}],
        "endpoints": [{"uid": "old-a", "ready": True}, {"uid": "old-b", "ready": True}],
    }
    down = {**baseline, "replicas": 0, "ready_replicas": 0, "pods": [], "endpoints": []}
    restored = {
        **baseline,
        "pods": [{"uid": "new-a", "ready": True}, {"uid": "new-b", "ready": True}],
        "endpoints": [{"uid": "new-a", "ready": True}, {"uid": "new-b", "ready": True}],
    }
    d["aps_initial"] = baseline
    d["samples"] = [
        {"observed_at": 1101.0, "aps": down, "pods": [], "observer_error": "APSUnavailable"},
        {"observed_at": 1145.0, "aps": down, "pods": [], "observer_error": "APSUnavailable"},
        {"observed_at": 1190.0, "aps": restored, "pods": [], "observer": d["final"]},
    ]
    d["reconciler"] = [
        {
            "activity": "close_session",
            "result": "OK",
            "scheduled_at": 1195.0,
            "completed_at": 1200.0,
            "scheduled_action": True,
        }
    ]
    return d


def test_aps_failure_does_not_skip_pod_deletion_and_schedule_retries_grant():
    assert evaluate_case("aps_down", aps_evidence())["result"] == "pass"


@pytest.mark.parametrize(
    "mutation",
    [
        "no_zero",
        "missing_stop_failure",
        "delete_after_restore",
        "no_reconcile",
        "wrong_uid",
        "not_ready",
        "config_drift",
    ],
)
def test_aps_outage_cannot_be_inferred_from_request_errors(mutation):
    d = aps_evidence()
    if mutation == "no_zero":
        d["samples"][0]["aps"]["pods"] = [{"uid": "lingering"}]
    elif mutation == "missing_stop_failure":
        d["history"][-3]["failure_type"] = None
    elif mutation == "delete_after_restore":
        d["history"][-1]["completed_at"] = 1199.0
    elif mutation == "no_reconcile":
        d["reconciler"] = []
    elif mutation == "wrong_uid":
        d["receipt"]["target_uid"] = "other"
    elif mutation == "not_ready":
        d["samples"][-1]["aps"]["ready_replicas"] = 1
    else:
        d["samples"][-1]["aps"]["spec_sha"] = "changed"
    assert evaluate_case("aps_down", d)["result"] == "fail"


def worker_evidence():
    d = evidence("worker_kill")
    d["receipt"].update(target_uid="worker-old", started_at=1105.0, restored_at=1110.0)
    d["worker_image"] = "digest-pinned"
    d["initial_worker"] = {"uid": "worker-old", "owner_uid": "replicaset-uid"}
    d["cleanup_gates"] = [
        {"stage": stage, "worker_uid": "worker-old", "session_id": "session", "observed_at": 1104.0}
        for stage in ("delete_sandbox", "delete_grant")
    ]
    d["worker_absence"] = {"uid": "worker-old", "http_status": 404}
    d["samples"] = [
        {
            "observed_at": 1110.0,
            "workers": [
                {
                    "uid": "worker-new",
                    "ready": True,
                    "image": "digest-pinned",
                    "owner_uid": "replicaset-uid",
                }
            ],
        }
    ]
    for row in d["history"][-2:]:
        row.update(
            result="OK" if row["activity"] == "delete_grant" else "DELETED",
            attempt=2,
            completed_at=1150.0,
        )
    return d


def test_worker_kill_is_mid_real_cleanup_and_actual_retries_complete():
    assert evaluate_case("worker_kill", worker_evidence())["result"] == "pass"


@pytest.mark.parametrize(
    "mutation",
    ["no_kill", "same_worker", "wrong_image", "no_stop_before", "missing_gate", "no_retry"],
)
def test_worker_replacement_evidence_must_be_exact(mutation):
    d = worker_evidence()
    if mutation == "no_kill":
        d["worker_absence"]["http_status"] = 200
    elif mutation == "same_worker":
        d["samples"][0]["workers"][0]["uid"] = "worker-old"
    elif mutation == "wrong_image":
        d["samples"][0]["workers"][0]["image"] = "other"
    elif mutation == "no_stop_before":
        d["history"][-3]["completed_at"] = 1120
    elif mutation == "missing_gate":
        d["cleanup_gates"].pop()
    else:
        d["history"][-1]["attempt"] = 1
    assert evaluate_case("worker_kill", d)["result"] == "fail"


def healthy_evidence(case="success"):
    d = evidence(case)
    d["history"][-1]["result"] = "DELETED"
    d["terminal"]["kind"] = "CANCELED" if case == "cancel" else "COMPLETED"
    d["progress"] = {"outcome": "COMPLETED", "generation": 1, "boundaries": 2}
    return d


@pytest.mark.parametrize("case", ["success", "cancel"])
def test_success_and_cancel_use_real_cleanup_activity_outcomes(case):
    d = healthy_evidence(case)
    assert evaluate_case(case, d)["result"] == "pass"
    d["history"][-2]["result"] = None
    assert evaluate_case(case, d)["result"] == "fail"


def test_retry_exhaustion_requires_three_unknown_outcomes_and_confirmed_replacements():
    d = healthy_evidence("retry_exhaustion")
    d["progress"] = {"outcome": "FAILED", "generation": 3, "boundaries": 1}
    d["history"][-1]["generation"] = 3
    for generation in (1, 2, 3):
        d["history"].append(
            {
                "activity": "run_boundary",
                "generation": generation,
                "boundary": 1,
                "failure_type": "OutcomeUnknown",
                "completed_at": 1000 + generation * 20,
            }
        )
        if generation < 3:
            d["history"].append(
                {
                    "activity": "delete_sandbox",
                    "generation": generation,
                    "result": "DELETED",
                    "completed_at": 1001 + generation * 20,
                }
            )
            d["history"].append(
                {
                    "activity": "set_generation",
                    "generation": generation + 1,
                    "scheduled_at": 1002 + generation * 20,
                    "completed_at": 1003 + generation * 20,
                }
            )
            d["history"].append(
                {
                    "activity": "create_sandbox",
                    "restore": "a" * 64,
                    "generation": generation + 1,
                    "scheduled_at": 1004 + generation * 20,
                    "completed_at": 1005 + generation * 20,
                }
            )
    assert evaluate_case("retry_exhaustion", d)["result"] == "pass"
    d["history"][-1]["failure_type"] = "Forbidden"
    assert evaluate_case("retry_exhaustion", d)["result"] == "fail"


@pytest.mark.parametrize(
    "mutation",
    [
        "no_restore_receipt",
        "late_restore",
        "fault_before_baseline",
        "instant_not_confirmed",
        "boolean_generation",
    ],
)
def test_fault_time_and_presence_types_cannot_be_inferred(mutation):
    d = evidence()
    if mutation == "no_restore_receipt":
        d["receipt"].pop("restored_at")
    elif mutation == "late_restore":
        d["receipt"]["restored_at"] = 1300
    elif mutation == "fault_before_baseline":
        d["receipt"]["started_at"] = 900
    elif mutation == "instant_not_confirmed":
        d["history"][-1]["completed_at"] = 1101
    else:
        d["final"]["generation"] = False
    assert evaluate_case("finalizer", d)["result"] == "fail"


@pytest.mark.parametrize("field", ["refresh_backed", "access_current"])
def test_long_record_does_not_replace_current_refresh_backed_initial_grant(field):
    d = evidence()
    d["initial"][field] = False
    assert evaluate_case("finalizer", d)["result"] == "fail"


def test_same_image_worker_from_another_controller_does_not_prove_recovery():
    d = worker_evidence()
    d["samples"][0]["workers"][0]["owner_uid"] = "other-controller"
    assert evaluate_case("worker_kill", d)["result"] == "fail"
