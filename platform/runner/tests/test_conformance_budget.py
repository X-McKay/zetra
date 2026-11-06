import copy
from datetime import datetime, timedelta, timezone

import pytest

from runner.conformance_budget import evaluate_budget

START = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)


def stamp(n):
    return (START + timedelta(seconds=n)).isoformat()


def uid(n):
    return f"00000000-0000-4000-8000-{n:012d}"


def evidence(case="day"):
    primary = uid(1)
    control = uid(2)
    labels = (
        [f"spend-{n}" for n in range(18)] + ["exhausted", "after_restart"]
        if case == "day"
        else ["warm"]
        + [f"concurrent-{n}" for n in range(10)]
        + [f"fill-{n}" for n in range(4)]
        + ["exhausted", "tampered", "restored"]
    )
    agent = "conformance-budget-" + ("day" if case == "day" else "session")
    reports = []
    aps = []
    backend = []
    observations = []
    for sid, names in [(primary, labels)] + (
        [] if case == "day" else [(control, ["control-before", "control-after"])]
    ):
        calls = []
        for i, label in enumerate(names):
            deny = (
                label in {"exhausted", "after_restart", "tampered", "restored"}
                or label.startswith("concurrent-")
                and int(label.split("-")[1]) >= 3
            )
            n = i + 100 * (sid == control)
            decision = uid(n + 100)
            trace = f"{n + 1:032x}"
            at = 10 + i * 10
            if label.startswith("concurrent-"):
                at = 20
            if label == "after_restart":
                at = 330
            if label == "control-before":
                at = 5
            if label == "control-after":
                at = 400
            generation = 2 if label == "restored" else 1
            calls.append(
                {
                    "label": label,
                    "trace_id": trace,
                    "status": 429 if deny else 200,
                    "started_at": stamp(at),
                    "finished_at": stamp(at + 1),
                    **(
                        {"reason": "deny.budget_exhausted", "decision_id": decision}
                        if deny
                        else {"input_tokens": 50, "output_tokens": 50, "total_tokens": 100}
                    ),
                }
            )
            aps.append(
                {
                    "ts": stamp(at),
                    "decision_id": decision,
                    "session_id": sid,
                    "agent_id": "tenant-test/" + agent,
                    "backend": "llm-budget-conformance",
                    "generation": generation,
                    "release": "fixture-release",
                    "policy_digest": "sha256:" + "a" * 64,
                    "reason": "deny.budget_exhausted" if deny else "allow",
                    "trace_id": trace,
                    "reserved_tokens": 0 if deny else 300,
                }
            )
            if not deny:
                backend.append(
                    {
                        "msg": "budget backend response",
                        "decision_id": decision,
                        "session_id": sid,
                        "agent_id": "tenant-test/" + agent,
                        "status": 200,
                        "method": "POST",
                        "path": "/v1/chat/completions",
                        "trace_id": trace,
                        "input_tokens": 50,
                        "output_tokens": 50,
                        "total_tokens": 100,
                        "max_total_tokens": 300,
                        "response_at": stamp(at + 1),
                    }
                )
                aps.append(
                    {
                        "msg": "budget settled",
                        "decision_id": decision,
                        "actual_tokens": 100,
                        "time": stamp(at + 2),
                    }
                )
        reports.append(
            {
                "session_id": sid,
                "case": case if sid == primary else "control",
                "outcome": "COMPLETED",
                "generation": 2 if case == "session" and sid == primary else 1,
                "release": "fixture-release",
                "snapshot": "a" * 64,
                "budget": {
                    "schema": 1,
                    "case": case if sid == primary else "control",
                    "calls": calls,
                    "tamper_before": 800,
                    "tamper_after": 0,
                    "restored_usage": 0,
                },
                "recovery": {
                    "confirmed_delete_before_fence_create": True,
                    "restored_committed_zero_checkpoint": True,
                },
            }
        )
        counts = [(0, 0), (1800 if case == "day" else 800 if sid == primary else 200, 0)]
        if case == "session" and sid == primary:
            counts = [(0, 0), (100, 900), (400, 0), (800, 0)]
        for n, (spent, inflight) in enumerate(counts):
            observations.append(
                {
                    "session_id": sid,
                    "agent": agent,
                    "tenant": "tenant-test",
                    "day_utc": "2026-11-06",
                    "generation": 1,
                    "observed_at": stamp(
                        0
                        if n == 0
                        else 20.5
                        if inflight
                        else 24
                        if spent == 400
                        else 450
                        if sid == control
                        else 350
                        if case == "day"
                        else 190
                    ),
                    "session": {"spent": spent, "inflight": inflight},
                    "day": {"spent": spent, "inflight": inflight},
                }
            )
    restart = {
        "before": [
            {"uid": "old1", "ready": True, "image_id": "image1"},
            {"uid": "old2", "ready": True, "image_id": "image1"},
        ],
        "after": [
            {"uid": "new1", "ready": True, "image_id": "image1"},
            {"uid": "new2", "ready": True, "image_id": "image1"},
        ],
        "old_uids_absent": True,
        "configuration_before": "b" * 64,
        "configuration_after": "b" * 64,
        "started_at": stamp(200),
        "finished_at": stamp(300),
    }
    return {
        "case": case,
        "sessions": reports,
        "aps": aps,
        "backend": backend,
        "observations": observations,
        "restart": restart,
    }


@pytest.mark.parametrize("case", ["day", "session"])
def test_exact_budget_evidence_passes(case):
    result = evaluate_budget(**evidence(case))
    assert result["result"] == "pass", result["checks"]


@pytest.mark.parametrize(
    "change",
    [
        "first_denied",
        "unreadable",
        "wrong_reserve",
        "late_settlement",
        "missing_settlement",
        "wrong_usage",
        "no_restart",
        "same_gateway",
        "dirty_baseline",
        "overshoot",
        "local_rate429",
    ],
)
def test_daily_missing_or_wrong_evidence_fails(change):
    data = evidence()
    if change == "first_denied":
        data["sessions"][0]["budget"]["calls"][0]["status"] = 429
    elif change == "unreadable":
        data["aps"].append({"msg": "log unavailable"})
    elif change == "wrong_reserve":
        data["aps"][0]["reserved_tokens"] = 131072
    elif change == "late_settlement":
        data["aps"][1]["time"] = stamp(20)
    elif change == "missing_settlement":
        data["aps"].pop(1)
    elif change == "wrong_usage":
        data["backend"][0]["total_tokens"] = 0
    elif change == "no_restart":
        data["restart"] = None
    elif change == "same_gateway":
        data["restart"]["after"] = copy.deepcopy(data["restart"]["before"])
    elif change == "dirty_baseline":
        data["observations"][0]["day"]["spent"] = 100
    elif change == "overshoot":
        data["observations"][-1]["day"]["spent"] = 2100
    elif change == "local_rate429":
        data["aps"][-2]["reason"] = "deny.rate_limit"
    assert evaluate_budget(**data)["result"] == "fail"


@pytest.mark.parametrize(
    "change",
    [
        "four_concurrent",
        "no_overlap",
        "no_inflight",
        "missing_restore",
        "fake_tamper",
        "control_denied",
        "duplicate_decision",
    ],
)
def test_session_failure_modes(change):
    data = evidence("session")
    if change == "four_concurrent":
        data["sessions"][0]["budget"]["calls"][4]["status"] = 200
    elif change == "no_overlap":
        data["aps"][1]["time"] = stamp(11)
        data["backend"][1]["response_at"] = stamp(19)
    elif change == "no_inflight":
        data["observations"].pop(1)
    elif change == "missing_restore":
        data["sessions"][0]["recovery"] = {}
    elif change == "fake_tamper":
        data["sessions"][0]["budget"]["tamper_after"] = 800
    elif change == "control_denied":
        data["sessions"][1]["budget"]["calls"][-1]["status"] = 429
    elif change == "duplicate_decision":
        data["aps"].append(copy.deepcopy(data["aps"][0]))
    assert evaluate_budget(**data)["result"] == "fail"


@pytest.mark.parametrize(
    "change", ["late_baseline", "early_final", "inflight_outside_window", "ledger_refund"]
)
def test_observation_timing_and_monotonic_spend_are_required(change):
    data = evidence("session")
    if change == "late_baseline":
        data["observations"][0]["observed_at"] = stamp(500)
    elif change == "early_final":
        data["observations"][3]["observed_at"] = stamp(0)
    elif change == "inflight_outside_window":
        data["observations"][1]["observed_at"] = stamp(500)
    elif change == "ledger_refund":
        data["observations"].insert(
            3, {**copy.deepcopy(data["observations"][2]), "session": {"spent": 0, "inflight": 0}}
        )
    assert evaluate_budget(**data)["result"] == "fail"


@pytest.mark.parametrize("source", ["backend", "other_aps_backend"])
def test_extra_session_request_evidence_never_passes(source):
    data = evidence()
    if source == "backend":
        extra = copy.deepcopy(data["backend"][0])
        extra["decision_id"] = uid(999)
        extra["trace_id"] = "f" * 32
        data["backend"].append(extra)
    else:
        extra = copy.deepcopy(data["aps"][0])
        extra["decision_id"] = uid(999)
        extra["trace_id"] = "f" * 32
        extra["backend"] = "llm-other"
        data["aps"].append(extra)
    assert evaluate_budget(**data)["result"] == "fail"
