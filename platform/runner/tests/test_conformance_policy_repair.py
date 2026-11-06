"""Full T28 requires concurrent real preflight and automatic UID-bound repair."""

import copy

import pytest

from runner.conformance_policy_repair import evaluate


def evidence():
    return {
        "tenant": "tenant-test",
        "session_id": "sid",
        "baseline": {"names": ["a", "b", "c", "d"], "digest": "sha256:baseline", "valid": True},
        "extra": {
            "name": "t28-extra",
            "uid": "extra-uid",
            "resource_version": "10",
            "creation_confirmed": True,
        },
        "drift_digest": "sha256:drift",
        "create_elapsed": 0.1,
        "repair": {
            "elapsed": 1.0,
            "valid": True,
            "extra_absent": True,
            "digest": "sha256:baseline",
        },
        "controller_before": [{"uid": "controller", "image_id": "image", "restarts": 0}],
        "controller_after": [{"uid": "controller", "image_id": "image", "restarts": 0}],
        "receipts": [
            {
                "event": "tenant_network_policy_deleted",
                "tenant": "tenant-test",
                "name": "t28-extra",
                "uid": "extra-uid",
                "resource_version": "10",
            }
        ],
        "session": {
            "outcome": "FAILED",
            "activities": ["preflight", "close_session"],
            "pods_absent": True,
            "failure": {
                "activity": "preflight",
                "type": "Forbidden",
                "non_retryable": True,
                "details": {
                    "kind": "effective_network_policy_mismatch",
                    "tenant": "tenant-test",
                    "session_id": "sid",
                    "digest": "sha256:drift",
                    "published_digest": "sha256:baseline",
                    "truncated": False,
                    "policies": [{"name": n, "uid": n} for n in ["a", "b", "c", "d"]]
                    + [{"name": "t28-extra", "uid": "extra-uid"}],
                },
            },
        },
        "cleanup": {
            "operator_deleted": False,
            "extra_absent": True,
            "session_closed": True,
            "session_pods_absent": True,
        },
    }


def test_complete_automatic_repair_and_concurrent_refusal_pass():
    assert evaluate(evidence())["result"] == "pass"


@pytest.mark.parametrize(
    "fault",
    [
        "late",
        "negative_elapsed",
        "missing_repair",
        "wrong_digest",
        "manual_cleanup",
        "missing_receipt",
        "wrong_uid",
        "controller_replaced",
        "raced_preflight",
        "wrong_failure",
        "wrong_session",
        "truncated",
        "sandbox_created",
        "dirty_baseline",
        "cleanup_failed",
    ],
)
def test_partial_or_raced_evidence_never_passes(fault):
    d = copy.deepcopy(evidence())
    if fault == "late":
        d["repair"]["elapsed"] = 60.01
    elif fault == "negative_elapsed":
        d["repair"]["elapsed"] = -1
    elif fault == "missing_repair":
        d["repair"] = None
    elif fault == "wrong_digest":
        d["repair"]["digest"] = "wrong"
    elif fault == "manual_cleanup":
        d["cleanup"]["operator_deleted"] = True
    elif fault == "missing_receipt":
        d["receipts"] = []
    elif fault == "wrong_uid":
        d["receipts"][0]["uid"] = "replacement"
    elif fault == "controller_replaced":
        d["controller_after"][0]["uid"] = "new"
    elif fault == "raced_preflight":
        d["session"]["failure"]["details"]["policies"].pop()
    elif fault == "wrong_failure":
        d["session"]["failure"]["type"] = "NotFound"
    elif fault == "wrong_session":
        d["session"]["failure"]["details"]["session_id"] = "other"
    elif fault == "truncated":
        d["session"]["failure"]["details"]["truncated"] = True
    elif fault == "sandbox_created":
        d["session"]["activities"].append("create_sandbox")
    elif fault == "dirty_baseline":
        d["baseline"]["valid"] = False
    elif fault == "cleanup_failed":
        d["cleanup"]["session_closed"] = False
    assert evaluate(d)["result"] == "fail"


def test_t28_cli_is_exclusive_and_explicit():
    from runner.conformance import parse_args

    base = [
        "--tests",
        "T28",
        "--release",
        "signed",
        "--allow-policy-repair-mutation",
        "--policy-repair-digest",
        "sha256:" + "a" * 64,
    ]
    assert parse_args(base).tests == ["T28"]
    for args in [
        base[:4],
        base + ["--tenant", "tenant-other"],
        base + ["--tests", "T28,T4"],
        base + ["--agent", "other"],
        base + ["--policy-repair-digest", "arbitrary"],
    ]:
        with pytest.raises(SystemExit):
            parse_args(args)
