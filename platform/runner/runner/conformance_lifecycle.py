"""T24 evidence verdicts: genuine held state, actual faults and independent cleanup."""

from __future__ import annotations

import math
import re

from runner.activities import DELETE_DEADLINE
from runner.conformance_lifecycle_control import CASES, FAULTS

FINALIZER = "agentplatform.io/conformance-lifecycle"


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def clean(observation):
    return (
        isinstance(observation, dict)
        and observation.get("generation_present") is False
        and type(observation.get("generation")) is int
        and observation.get("generation") == 0
        and observation.get("grant_present") is False
        and observation.get("vault_present") is False
        and observation.get("stopped") is True
        and observation.get("cleanup_pending") is False
    )


def held(observation):
    return (
        isinstance(observation, dict)
        and observation.get("generation_present") is True
        and type(observation.get("generation")) is int
        and observation.get("generation") == 1
        and observation.get("grant_present") is True
        and observation.get("vault_present") is True
        and observation.get("refresh_backed") is True
        and observation.get("access_current") is True
        and observation.get("stopped") is False
        and observation.get("cleanup_pending") is False
        and all(
            type(observation.get(k)) is int and observation[k] > 600
            for k in ("grant_ttl_seconds", "vault_ttl_seconds")
        )
    )


def result(rows, activity, expected):
    return any(
        r.get("activity") == activity
        and r.get("result") == expected
        and finite(r.get("completed_at"))
        for r in rows
    )


def evaluate_case(case, data):
    initial, final = data.get("initial", {}), data.get("final", {})
    session = data.get("session_id")
    history, reconciler = data.get("history", []), data.get("reconciler", [])
    terminal, gate = data.get("terminal", {}), data.get("baseline_gate", {})
    samples, receipt = data.get("samples", []), data.get("receipt") or {}
    start, end, anchor = initial.get("observed_at"), final.get("observed_at"), data.get("anchor")
    elapsed = data.get("elapsed_monotonic")
    times = all(finite(t) for t in (start, end, anchor, elapsed))
    runs = [r for r in history if r.get("activity") == "run_boundary" and r.get("boundary") == 0]
    snapshots = [
        r for r in history if r.get("activity") == "snapshot_workspace" and r.get("boundary") == 0
    ]
    initial_pods = data.get("initial_pods", [])
    known_names = {p.get("name") for p in initial_pods} | {
        r.get("result")
        for r in history
        if r.get("activity") == "create_sandbox" and isinstance(r.get("result"), str)
    }
    absence = data.get("pod_absence", [])
    checks = {
        "bound_case": case in CASES and data.get("case") == case,
        "same_trusted_session": bool(session)
        and all(
            o.get("session_id") == session
            and o.get("tenant") == "tenant-test"
            and o.get("agent") == "conformance-lifecycle"
            for o in (initial, final)
        ),
        "genuine_grant_and_vault_initially_held": held(initial),
        "generation_grant_vault_and_pending_absent": clean(final),
        "cleanup_within_ten_minutes": times and 0 <= end - anchor <= 600 and 0 <= elapsed <= 600,
        "expiry_did_not_substitute_for_cleanup": times
        and start <= anchor <= end
        and all(
            type(initial.get(k)) is int and end < start + initial[k]
            for k in ("grant_ttl_seconds", "vault_ttl_seconds")
        ),
        "trusted_initial_checkpoint": bool(runs)
        and bool(snapshots)
        and runs[0].get("generation") == snapshots[0].get("generation") == 1
        and runs[0].get("result", {}).get("done") is False
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(snapshots[0].get("result", ""))))
        and finite(runs[0].get("completed_at"))
        and finite(snapshots[0].get("completed_at"))
        and runs[0]["completed_at"]
        < snapshots[0]["scheduled_at"]
        <= snapshots[0]["completed_at"]
        <= start,
        "actual_initial_gate": gate.get("stage") == "boundary1"
        and gate.get("session_id") == session
        and gate.get("generation") == 1
        and bool(gate.get("worker_uid")),
        "all_session_pods_confirmed_gone": bool(initial_pods)
        and data.get("final_pods") == []
        and len(absence) == len(known_names)
        and {p.get("name") for p in absence} == known_names
        and all(p.get("http_status") == 404 for p in absence),
        "restoration_observed": data.get("restored") is True,
        "all_required_evidence_readable": data.get("evidence_errors") == [],
    }
    if case in {"aps_down", "finalizer", "worker_kill", "activity_crash"}:
        checks["operator_receipt_time_bound"] = (
            times
            and all(finite(receipt.get(k)) for k in ("started_at", "restored_at"))
            and start <= receipt["started_at"] <= receipt["restored_at"] <= end
        )
    scheduled = [
        r
        for r in reconciler
        if r.get("scheduled_action") is True
        and finite(r.get("scheduled_at"))
        and finite(anchor)
        and r["scheduled_at"] >= anchor
    ]
    if case == "terminate":
        checks.update(
            {
                "actual_hard_termination": terminal.get("kind") == "TERMINATED"
                and terminal.get("at") == anchor,
                "scheduled_reconciler_recorded_cleanup": result(scheduled, "close_session", "OK")
                and result(scheduled, "delete_orphan", "DELETED"),
            }
        )
    elif case == "finalizer":
        blocked = [
            s
            for s in samples
            if any(
                p.get("uid") == receipt.get("target_uid")
                and p.get("deleting") is True
                and FINALIZER in p.get("finalizers", [])
                for p in s.get("pods", [])
            )
        ]
        checks.update(
            {
                "operator_fault_is_bound": receipt.get("session_id") == session
                and receipt.get("fault") == case
                and receipt.get("target_uid") in {p.get("uid") for p in initial_pods},
                "real_deletion_blocked": bool(blocked)
                and any(
                    r.get("activity") == "delete_sandbox"
                    and r.get("result") == "NOT_CONFIRMED"
                    and finite(r.get("scheduled_at"))
                    and finite(r.get("completed_at"))
                    and r["completed_at"] - r["scheduled_at"] >= DELETE_DEADLINE - 2
                    for r in history
                ),
                "stop_and_grant_independent_of_blocked_delete": any(
                    clean(s.get("observer")) for s in blocked
                )
                and result(history, "stop_session", "OK")
                and result(history, "delete_grant", "OK"),
                "workflow_closed": terminal.get("kind") == "COMPLETED",
            }
        )
    elif case == "aps_down":
        before = data.get("aps_initial", {})
        zeros = [s for s in samples if aps_empty(s.get("aps", {}))]
        zero_times = [s["observed_at"] for s in zeros if finite(s.get("observed_at"))]
        restored_samples = [s for s in samples if aps_ready(s.get("aps", {}))]
        zero_start, zero_end = (min(zero_times), max(zero_times)) if zero_times else (0, 0)
        failed = [
            r
            for r in history
            if r.get("activity") in {"stop_session", "delete_grant"}
            and r.get("failure_type") == "APSUnavailable"
            and finite(r.get("completed_at"))
            and zero_start <= r["completed_at"] <= zero_end
        ]
        checks.update(
            {
                "operator_fault_is_bound": receipt.get("session_id") == session
                and receipt.get("fault") == case
                and receipt.get("target_uid") == before.get("uid")
                and bool(before.get("uid")),
                "actual_zero_pods_and_endpoints": len(zero_times) >= 2 and zero_end > zero_start,
                "both_real_cleanup_calls_failed_during_outage": {r["activity"] for r in failed}
                == {"stop_session", "delete_grant"},
                "pod_delete_independent_of_aps_failure": any(
                    r.get("activity") == "delete_sandbox"
                    and r.get("result") == "DELETED"
                    and finite(r.get("completed_at"))
                    and zero_start <= r["completed_at"] <= zero_end
                    for r in history
                ),
                "same_aps_configuration_restored": aps_ready(before)
                and bool(restored_samples)
                and all(
                    s.get("aps", {}).get("uid") == before.get("uid")
                    and s.get("aps", {}).get("spec_sha") == before.get("spec_sha")
                    for s in samples
                    if "aps" in s
                )
                and restored_samples[-1]["observed_at"] > zero_end,
                "scheduled_reconciler_recorded_cleanup": result(scheduled, "close_session", "OK"),
                "workflow_closed": terminal.get("kind") == "COMPLETED",
            }
        )
    elif case == "worker_kill":
        checks.update(worker_replaced(data))
        kill_time = receipt.get("started_at")
        gates = data.get("cleanup_gates", [])
        checks.update(
            {
                "real_stop_completed_before_worker_loss": any(
                    r.get("activity") == "stop_session"
                    and r.get("result") == "OK"
                    and finite(r.get("completed_at"))
                    and finite(kill_time)
                    and r["completed_at"] < kill_time
                    for r in history
                ),
                "remaining_cleanup_was_running_on_killed_worker": {g.get("stage") for g in gates}
                == {"delete_sandbox", "delete_grant"}
                and len(gates) == 2
                and all(
                    g.get("worker_uid") == receipt.get("target_uid")
                    and g.get("session_id") == session
                    and finite(g.get("observed_at"))
                    and finite(kill_time)
                    and g["observed_at"] < kill_time
                    for g in gates
                ),
                "ordinary_cleanup_retries_completed": all(
                    any(
                        r.get("activity") == activity
                        and r.get("result") == expected
                        and type(r.get("attempt")) is int
                        and r["attempt"] >= 2
                        and finite(r.get("completed_at"))
                        and finite(kill_time)
                        and r["completed_at"] > kill_time
                        for r in history
                    )
                    for activity, expected in (
                        ("delete_grant", "OK"),
                        ("delete_sandbox", "DELETED"),
                    )
                ),
                "workflow_closed": terminal.get("kind") == "COMPLETED",
            }
        )
    elif case in {"success", "cancel", "retry_exhaustion", "activity_crash"}:
        checks.update(
            {
                "real_cleanup_activity_outcomes": all(
                    result(history, a, expected)
                    for a, expected in (
                        ("stop_session", "OK"),
                        ("delete_grant", "OK"),
                        ("delete_sandbox", "DELETED"),
                    )
                ),
                "expected_terminal": terminal.get("kind")
                == ("CANCELED" if case == "cancel" else "COMPLETED"),
            }
        )
        if case == "success":
            checks["completed_two_boundaries"] = data.get("progress") == {
                "outcome": "COMPLETED",
                "generation": 1,
                "boundaries": 2,
            }
        elif case in {"retry_exhaustion", "activity_crash"}:
            maximum = 3 if case == "retry_exhaustion" else 2
            failures = [
                r
                for r in history
                if r.get("activity") == "run_boundary"
                and r.get("boundary") == 1
                and r.get("failure_type")
            ]
            deletes = {
                r.get("generation"): r for r in history if r.get("activity") == "delete_sandbox"
            }
            fences = {
                r.get("generation"): r for r in history if r.get("activity") == "set_generation"
            }
            creates = {
                r.get("generation"): r for r in history if r.get("activity") == "create_sandbox"
            }
            checkpoint = snapshots[0].get("result") if snapshots else None
            checks["exact_recovery_failure_count"] = [
                (r.get("generation"), r.get("failure_type")) for r in failures
            ] == (
                [(1, "OutcomeUnknown"), (2, "OutcomeUnknown"), (3, "OutcomeUnknown")]
                if maximum == 3
                else [(1, "TimedOut")]
            )
            checks["replacement_only_after_confirmed_delete_and_fence"] = all(
                deletes.get(g - 1, {}).get("result") == "DELETED"
                and all(
                    finite(t)
                    for t in (
                        deletes.get(g - 1, {}).get("completed_at"),
                        fences.get(g, {}).get("scheduled_at"),
                        fences.get(g, {}).get("completed_at"),
                        creates.get(g, {}).get("scheduled_at"),
                    )
                )
                and deletes[g - 1]["completed_at"]
                < fences[g]["scheduled_at"]
                <= fences[g]["completed_at"]
                < creates[g]["scheduled_at"]
                and creates[g].get("restore") == checkpoint
                for g in range(2, maximum + 1)
            )
            checks["expected_recovery_progress"] = data.get("progress") == {
                "outcome": "FAILED" if maximum == 3 else "COMPLETED",
                "generation": maximum,
                "boundaries": 1 if maximum == 3 else 2,
            }
            if case == "activity_crash":
                checks.update(worker_replaced(data))
    else:
        checks["case_specific_fault_proven"] = False
    return {
        "result": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "evidence": data,
        "scope": "T24 qualification" if case in FAULTS else "supplemental lifecycle evidence",
    }


def aps_empty(sample):
    return (
        sample.get("replicas") == 0 and sample.get("pods") == [] and sample.get("endpoints") == []
    )


def aps_ready(sample):
    pods, endpoints = sample.get("pods", []), sample.get("endpoints", [])
    return (
        sample.get("replicas") == 2
        and sample.get("ready_replicas") == 2
        and len(pods) == 2
        and len(endpoints) == 2
        and all(p.get("ready") is True for p in [*pods, *endpoints])
        and len({p.get("uid") for p in pods}) == 2
        and {p.get("uid") for p in pods} == {p.get("uid") for p in endpoints}
    )


def worker_replaced(data):
    receipt = data.get("receipt") or {}
    uid = data.get("baseline_gate", {}).get("worker_uid")
    kill_time = receipt.get("started_at")
    absence = data.get("worker_absence", {})
    after = [
        s
        for s in data.get("samples", [])
        if finite(s.get("observed_at"))
        and finite(kill_time)
        and s["observed_at"] > kill_time
        and "workers" in s
    ]
    return {
        "exact_worker_was_killed": bool(uid)
        and receipt.get("target_uid") == uid
        and receipt.get("session_id") == data.get("session_id")
        and receipt.get("fault") == data.get("case")
        and absence.get("uid") == uid
        and absence.get("http_status") == 404,
        "same_image_replacement_ready": bool(after)
        and any(
            len(s["workers"]) == 1
            and s["workers"][0].get("uid") != uid
            and bool(s["workers"][0].get("uid"))
            and s["workers"][0].get("ready") is True
            and s["workers"][0].get("image") == data.get("worker_image")
            and bool(data.get("initial_worker", {}).get("owner_uid"))
            and s["workers"][0].get("owner_uid") == data["initial_worker"]["owner_uid"]
            for s in after
        ),
    }
