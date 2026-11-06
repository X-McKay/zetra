"""T21 separates APS-only fencing from confirmed-delete recovery; no fake successes."""

from __future__ import annotations

import math

from runner.activities import DELETE_DEADLINE
from runner.conformance_generation_control import FINALIZER


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def evaluate_case(case, data):
    history = data.get("history") or []
    initial = data.get("initial_pod") or {}
    cleanup = data.get("cleanup") or {}
    samples = data.get("samples") or []
    receipts = data.get("worker_receipts") or []
    g2 = [
        h
        for h in history
        if type(h.get("generation")) is int
        and h["generation"] >= 2
        and h.get("activity") in {"set_generation", "create_sandbox", "wait_ready", "run_boundary"}
    ]
    checks = {
        "no_runtime_error": "error_type" not in data
        and not any(k.endswith("error_type") for k in cleanup),
        "exact_initial_running_generation_1": bool(initial.get("uid"))
        and initial.get("generation") == 1
        and initial.get("phase") == "Running"
        and not initial.get("deleting"),
        "isolated_worker_stable": bool(data.get("worker_before"))
        and data.get("worker_before") == data.get("worker_after"),
        "ordinary_cleanup_confirmed": cleanup.get("session_closed") is True
        and cleanup.get("pods_absent") is True
        and cleanup.get("finalizer_absent") is True,
    }
    if case in {"blocked_deletion", "recovery_control"}:
        failures = [
            h
            for h in history
            if h.get("activity") == "run_boundary"
            and h.get("generation") == 1
            and h.get("failure_type")
            in {"SandboxLost", "ExecFailed", "OutcomeUnknown", "SnapshotFailed"}
        ]
        deletes = [
            h for h in history if h.get("activity") == "delete_sandbox" and h.get("generation") == 1
        ]
        checks["real_exec_failure_triggered_ordinary_recovery"] = any(
            type(f.get("completed_id")) is int
            and type(d.get("scheduled_id")) is int
            and f["completed_id"] < d["scheduled_id"]
            for f in failures
            for d in deletes
        )
    if case == "fence_only":
        proof = [
            r
            for r in receipts
            if r.get("operation") == "fence_generation_2"
            and r.get("session_id") == data.get("session_id")
            and r.get("pod_uid") == initial.get("uid")
            and r.get("generation") == 2
            and r.get("acknowledged") is True
        ]
        before, denied = data.get("allowed_requests") or [], data.get("stale_requests") or []
        sources = set(data.get("aps_replicas") or [])
        jtIs = {r.get("token_jti") for r in before}
        checks.update(
            {
                "trusted_fence_only_acknowledged": len(proof) == 1,
                "same_fixed_token_allowed_then_denied_by_both_replicas": len(sources) == 2
                and {
                    r.get("source_pod")
                    for r in before
                    if r.get("status") == 200 and r.get("reason") == "allow"
                }
                == sources
                and len(jtIs) == 1
                and None not in jtIs
                and {
                    r.get("source_pod")
                    for r in denied
                    if r.get("status") == 403
                    and r.get("reason") == "deny.generation_stale"
                    and r.get("generation") == 1
                    and r.get("token_jti") in jtIs
                }
                == sources,
                "generation_1_still_running_during_stale_requests": any(
                    s.get("phase") == "stale_denial"
                    and len(s.get("pods", [])) == 1
                    and s["pods"][0].get("uid") == initial.get("uid")
                    and s["pods"][0].get("phase") == "Running"
                    and not s["pods"][0].get("deleting")
                    for s in samples
                ),
                "no_replacement_provisioned": not g2
                and all(p.get("generation") == 1 for s in samples for p in s.get("pods", [])),
            }
        )
    elif case == "blocked_deletion":
        blocked = [
            s
            for s in samples
            if any(
                p.get("uid") == initial.get("uid")
                and p.get("deleting") is True
                and FINALIZER in p.get("finalizers", [])
                for p in s.get("pods", [])
            )
        ]
        removal = data.get("finalizer_receipt") or {}
        elapsed = data.get("reconciler_after_removal_seconds")
        checks.update(
            {
                "exact_finalizer_really_blocked_deletion": bool(blocked)
                and any(
                    r.get("operation") == "delete_for_recovery"
                    and r.get("pod_uid") == initial.get("uid")
                    and r.get("observed") == "terminated_and_held"
                    and r.get("acknowledged") is True
                    for r in receipts
                )
                and removal.get("target_uid") == initial.get("uid")
                and removal.get("finalizer_added") is True
                and removal.get("finalizer_removed") is True
                and removal.get("hold_expired") is False
                and removal.get("operator_errors") == [],
                "ordinary_recovery_stopped_not_confirmed": data.get("outcome") == "NOT_CONFIRMED"
                and any(
                    h.get("activity") == "delete_sandbox"
                    and h.get("result") == "NOT_CONFIRMED"
                    and finite(h.get("duration"))
                    and h["duration"] >= DELETE_DEADLINE
                    for h in history
                ),
                "never_provisioned_generation_2": not g2
                and all(p.get("generation") == 1 for s in samples for p in s.get("pods", [])),
                "scheduled_reconciler_confirmed_deletion_within_300s": data.get(
                    "harness_monotonic_bound"
                )
                is True
                and data.get("scheduled_gate_before_removal") is True
                and finite(elapsed)
                and 0 <= elapsed <= 300
                and any(
                    r.get("activity") == "delete_orphan"
                    and r.get("result") == "DELETED"
                    and r.get("scheduled_action") is True
                    and r.get("pod_name") == initial.get("name")
                    for r in data.get("reconciler", [])
                ),
            }
        )
    elif case == "recovery_control":
        deletes = [
            h
            for h in history
            if h.get("activity") == "delete_sandbox"
            and h.get("generation") == 1
            and h.get("result") == "DELETED"
        ]
        creates = [h for h in g2 if h.get("activity") == "create_sandbox"]
        checks.update(
            {
                "real_404_before_generation_2_provisioning": bool(deletes)
                and bool(creates)
                and all(
                    isinstance(c.get("result"), str)
                    and c["result"]
                    and c["result"] != initial.get("uid")
                    for c in creates
                )
                and all(
                    type(d.get("completed_id")) is int
                    and type(c.get("scheduled_id")) is int
                    and d["completed_id"] < c["scheduled_id"]
                    for d in deletes
                    for c in creates
                )
                and any(
                    r.get("operation") == "delete_for_recovery"
                    and r.get("pod_uid") == initial.get("uid")
                    and r.get("observed") == "404"
                    for r in receipts
                ),
                "generation_2_really_completed": all(h.get("generation") == 2 for h in g2)
                and data.get("outcome") == "COMPLETED"
                and data.get("generation") == 2
                and data.get("boundaries") == 3
                and any(
                    h.get("activity") == "run_boundary"
                    and h.get("generation") == 2
                    and h.get("result", {}).get("done") is True
                    for h in history
                    if isinstance(h.get("result"), dict)
                ),
            }
        )
    else:
        raise ValueError("unknown T21 case")
    return {
        "case": case,
        "result": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "evidence": data,
    }
