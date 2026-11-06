"""Exact T13/T13b using a dedicated, enforced 300-byte-token backend.

This qualifies the gateway/APS/Redis budget path, not Qwen token accounting.
Trusted observations and raw committed fixture evidence are both required.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tarfile
from datetime import datetime
from pathlib import Path

BACKEND = "llm-budget-conformance"
TESTS = ("T13", "T13b")
AGENTS = {"day": "conformance-budget-day", "session": "conformance-budget-session"}
DAY_AGENTS = (AGENTS["day"], "conformance-budget-day-retry", "conformance-budget-day-retry2")
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
TRACE = re.compile(r"[0-9a-f]{32}\Z")


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else math.nan
    except (AttributeError, TypeError, ValueError):
        return math.nan


def integers(row, **expected):
    return isinstance(row, dict) and all(
        type(row.get(key)) is int and row[key] == value for key, value in expected.items()
    )


def read_budget_state(artifact_dir, snapshot):
    """Read one bounded regular state member from a SHA256-verified committed archive."""
    if not isinstance(snapshot, str) or not SHA.fullmatch(snapshot):
        raise ValueError("invalid snapshot")
    path = Path(artifact_dir) / snapshot[:2] / snapshot
    with path.open("rb") as source:
        digest = hashlib.sha256()
        size = 0
        while chunk := source.read(1 << 20):
            size += len(chunk)
            if size > 64 << 20:
                raise ValueError("snapshot too large")
            digest.update(chunk)
        if digest.hexdigest() != snapshot:
            raise ValueError("snapshot digest mismatch")
        source.seek(0)
        with tarfile.open(fileobj=source, mode="r:") as archive:
            members = [m for m in archive if m.name == "state/state.json"]
            if len(members) != 1 or not members[0].isfile() or members[0].size > 1 << 20:
                raise ValueError("invalid state member")
            state = json.load(archive.extractfile(members[0]))
    if not isinstance(state, dict) or not isinstance(state.get("budget"), dict):
        raise ValueError("invalid budget state")
    return state


def gateway_replaced(restart, denied_at, retry_at):
    if not isinstance(restart, dict):
        return False
    before, after = restart.get("before", []), restart.get("after", [])
    if len(before) != 2 or len(after) != 2:
        return False
    old = {p.get("uid") for p in before}
    new = {p.get("uid") for p in after}
    return (
        len(old) == len(new) == 2
        and None not in old | new
        and not old & new
        and all(p.get("ready") is True and p.get("image_id") for p in before + after)
        and {p["image_id"] for p in before} == {p["image_id"] for p in after}
        and restart.get("old_uids_absent") is True
        and restart.get("completed") is True
        and type(restart.get("emitted_ns")) is int
        and restart["emitted_ns"] > 0
        and type(restart.get("deadline_seconds")) is int
        and restart["deadline_seconds"] == 100
        and type(restart.get("elapsed_seconds")) in (int, float)
        and 0 <= restart["elapsed_seconds"] < 100
        and isinstance(restart.get("configuration_before"), str)
        and bool(SHA.fullmatch(restart["configuration_before"]))
        and restart["configuration_before"] == restart.get("configuration_after")
        and denied_at
        <= timestamp(restart.get("started_at"))
        <= timestamp(restart.get("finished_at"))
        < retry_at
    )


def budget_agent(case, agent=None):
    selected = AGENTS[case] if agent is None else agent
    allowed = DAY_AGENTS if case == "day" else (AGENTS["session"],)
    if selected not in allowed:
        raise ValueError("budget identity is not an explicit dedicated fixture")
    return selected


def evaluate_budget(*, case, sessions, aps, backend, observations, restart=None, agent=None):
    agent = budget_agent(case, agent)
    primary = sessions[0] if sessions else {}
    report = primary.get("budget", {})
    calls = report.get("calls", [])
    control = sessions[1] if len(sessions) > 1 else {}
    ccalls = control.get("budget", {}).get("calls", [])
    all_calls = [(s, c) for s in sessions for c in s.get("budget", {}).get("calls", [])]
    ids = {s.get("session_id") for s in sessions}
    decisions = [
        d for d in aps if d.get("session_id") in ids and "decision_id" in d and "reason" in d
    ]
    traces = [c.get("trace_id") for _, c in all_calls]
    checks = {
        "completed_signed_fixture_sessions": len(sessions) == (1 if case == "day" else 2)
        and all(
            s.get("outcome") == "COMPLETED"
            and isinstance(s.get("snapshot"), str)
            and SHA.fullmatch(s["snapshot"])
            and isinstance(s.get("session_id"), str)
            and UUID.fullmatch(s["session_id"])
            for s in sessions
        ),
        "distinct_session_ids": len(ids) == len(sessions),
        "exact_fixture_cases": report.get("schema") == 1
        and report.get("case") == case
        and (case == "day" or control.get("budget", {}).get("case") == "control"),
        "all_logs_readable": not any(e.get("msg") == "log unavailable" for e in aps + backend),
        "unique_request_traces": bool(traces)
        and all(isinstance(t, str) and TRACE.fullmatch(t) for t in traces)
        and len(set(traces)) == len(traces),
        "exactly_one_aps_decision_per_request": len(decisions) == len(all_calls)
        and len({d.get("decision_id") for d in decisions}) == len(all_calls),
        "http_and_aps_match": True,
        "enforced_reserve_300": True,
        "computed_backend_usage_100": True,
        "settlement_within_five_seconds": True,
    }
    links = []
    matched = {}
    for session, call in all_calls:
        rows = [
            d
            for d in decisions
            if d.get("trace_id") == call.get("trace_id")
            and d.get("session_id") == session.get("session_id")
        ]
        if len(rows) != 1:
            checks["exactly_one_aps_decision_per_request"] = False
            continue
        d = rows[0]
        decision = d.get("decision_id")
        matched[call.get("trace_id")] = d
        status = call.get("status")
        allow = status == 200 and d.get("reason") == "allow"
        deny = (
            status == 429
            and d.get("reason") == "deny.budget_exhausted"
            and call.get("reason") == "deny.budget_exhausted"
            and call.get("decision_id") == decision
        )
        checks["http_and_aps_match"] &= (
            (allow or deny)
            and d.get("backend") == BACKEND
            and d.get("agent_id") == "tenant-test/" + agent
            and d.get("release") == session.get("release")
            and isinstance(decision, str)
            and bool(UUID.fullmatch(decision))
        )
        captures = [b for b in backend if b.get("decision_id") == decision]
        settlements = [
            a for a in aps if a.get("msg") == "budget settled" and a.get("decision_id") == decision
        ]
        delay = None
        if allow:
            checks["enforced_reserve_300"] &= integers(d, reserved_tokens=300)
            checks["computed_backend_usage_100"] &= (
                integers(call, input_tokens=50, output_tokens=50, total_tokens=100)
                and len(captures) == 1
                and integers(
                    captures[0],
                    input_tokens=50,
                    output_tokens=50,
                    total_tokens=100,
                    max_total_tokens=300,
                    status=200,
                )
                and captures[0].get("session_id") == session.get("session_id")
                and captures[0].get("agent_id") == "tenant-test/" + agent
                and captures[0].get("trace_id") == call.get("trace_id")
                and captures[0].get("method") == "POST"
                and captures[0].get("path") == "/v1/chat/completions"
            )
            if len(settlements) == len(captures) == 1:
                delay = timestamp(settlements[0].get("time")) - timestamp(
                    captures[0].get("response_at")
                )
            checks["settlement_within_five_seconds"] &= (
                delay is not None
                and 0 <= delay <= 5
                and integers(settlements[0], actual_tokens=100)
            )
        else:
            checks["computed_backend_usage_100"] &= not captures and not settlements
        links.append(
            {
                "label": call.get("label")
                if isinstance(call.get("label"), str)
                and re.fullmatch(r"[a-z]+(?:-[a-z0-9]+)*", call["label"])
                else "invalid",
                "status": status if type(status) is int else None,
                "decision_id": decision
                if isinstance(decision, str) and UUID.fullmatch(decision)
                else None,
                "settlement_seconds": delay if delay is not None and math.isfinite(delay) else None,
            }
        )
    allowed_ids = {d.get("decision_id") for d in matched.values() if d.get("reason") == "allow"}
    session_captures = [b for b in backend if b.get("session_id") in ids]
    checks["no_unmatched_backend_requests"] = (
        len(session_captures) == len(allowed_ids)
        and {b.get("decision_id") for b in session_captures} == allowed_ids
    )
    labels = [c.get("label") for c in calls]
    expected = (
        [f"spend-{n}" for n in range(18)] + ["exhausted", "after_restart"]
        if case == "day"
        else ["warm"]
        + [f"concurrent-{n}" for n in range(10)]
        + [f"fill-{n}" for n in range(4)]
        + ["exhausted", "tampered", "restored"]
    )
    checks["exact_request_sequence"] = labels == expected
    checks["single_release_policy"] = len({d.get("policy_digest") for d in decisions}) == 1 and all(
        isinstance(d.get("policy_digest"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", d["policy_digest"])
        for d in decisions
    )
    samples = [o for o in observations if o.get("session_id") == primary.get("session_id")]
    checks["trusted_zero_baseline"] = (
        bool(samples)
        and integers(samples[0].get("session"), spent=0, inflight=0)
        and bool(observations)
        and integers(observations[0].get("day"), spent=0, inflight=0)
    )
    all_samples_valid = bool(observations)
    for o in observations:
        all_samples_valid &= (
            o.get("tenant") == "tenant-test"
            and o.get("agent") == agent
            and o.get("session_id") in ids
            and o.get("day_utc") == samples[0].get("day_utc")
            if samples
            else False
        )
        for name, limit in (
            ("session", 200000 if case == "day" else 1000),
            ("day", 2000 if case == "day" else 2000000),
        ):
            ledger = o.get(name, {})
            all_samples_valid &= (
                all(type(ledger.get(k)) is int and ledger[k] >= 0 for k in ("spent", "inflight"))
                and ledger.get("spent", limit + 1) + ledger.get("inflight", limit + 1) <= limit
            )
    checks["ledger_never_exceeds_exact_limits"] = all_samples_valid
    checks["observations_cover_requests_in_order"] = True
    checks["spent_never_refunded"] = True
    for session in sessions:
        observed = [o for o in observations if o.get("session_id") == session.get("session_id")]
        requested = session.get("budget", {}).get("calls", [])
        times = [timestamp(o.get("observed_at")) for o in observed]
        checks["observations_cover_requests_in_order"] &= (
            bool(observed and requested)
            and all(math.isfinite(t) for t in times)
            and times == sorted(times)
            and times[0] < timestamp(requested[0].get("started_at"))
            and times[-1] >= timestamp(requested[-1].get("finished_at"))
        )
        for ledger_name in ("session", "day"):
            spent = [o.get(ledger_name, {}).get("spent") for o in observed]
            checks["spent_never_refunded"] &= (
                bool(spent) and all(type(n) is int for n in spent) and spent == sorted(spent)
            )
    checks["actual_usage_not_first_call_denial"] = sum(
        c.get("total_tokens", 0) for c in calls if c.get("status") == 200
    ) == (1800 if case == "day" else 800)
    checks["final_ledger_exact"] = bool(samples) and integers(
        samples[-1].get("session"), spent=1800 if case == "day" else 800, inflight=0
    )
    if case == "day":
        checks["eighteen_successes_then_two_budget_refusals"] = [
            c.get("status") for c in calls
        ] == [200] * 18 + [429, 429]
        checks["day_spent_1800"] = bool(samples) and integers(
            samples[-1].get("day"), spent=1800, inflight=0
        )
        checks["both_gateway_replicas_replaced_before_retry"] = len(
            calls
        ) == 20 and gateway_replaced(
            restart, timestamp(calls[-2].get("finished_at")), timestamp(calls[-1].get("started_at"))
        )
        checks["generation_unchanged"] = primary.get("generation") == 1 and all(
            d.get("generation") == 1 for d in decisions
        )
    else:
        concurrent = calls[1:11]
        concurrent_decisions = [matched.get(c.get("trace_id"), {}) for c in concurrent]
        concurrent_ids = {d.get("decision_id") for d in concurrent_decisions}
        replies = [
            timestamp(b.get("response_at"))
            for b in backend
            if b.get("decision_id") in concurrent_ids
        ]
        checks["ten_concurrent_exact_floor_three"] = (
            len(concurrent) == 10
            and sorted(c.get("status", 0) for c in concurrent) == [200] * 3 + [429] * 7
        )
        checks["all_ten_decisions_before_first_response"] = len(replies) == 3 and all(
            timestamp(d.get("ts")) < min(replies) for d in concurrent_decisions
        )
        checks["trusted_900_tokens_inflight"] = any(
            integers(s.get("session"), spent=100, inflight=900)
            and len(replies) == 3
            and max(timestamp(d.get("ts")) for d in concurrent_decisions)
            <= timestamp(s.get("observed_at"))
            < min(replies)
            for s in samples
        )
        checks["settled_concurrency_to_400"] = any(
            integers(s.get("session"), spent=400, inflight=0)
            and len(replies) == 3
            and max(replies) <= timestamp(s.get("observed_at")) <= max(replies) + 5
            for s in samples
        )
        checks["four_fills_and_three_refusals"] = [c.get("status") for c in calls[11:]] == [
            200
        ] * 4 + [429] * 3
        checks["actual_state_tampered_and_restored_zero"] = integers(
            report, tamper_before=800, tamper_after=0, restored_usage=0
        )
        checks["normal_generation_two_restore"] = (
            primary.get("generation") == 2
            and primary.get("recovery", {}).get("confirmed_delete_before_fence_create") is True
            and primary.get("recovery", {}).get("restored_committed_zero_checkpoint") is True
            and bool(calls)
            and matched.get(calls[-1].get("trace_id"), {}).get("generation") == 2
        )
        checks["independent_second_session"] = (
            len(ccalls) == 2
            and [c.get("label") for c in ccalls] == ["control-before", "control-after"]
            and all(c.get("status") == 200 for c in ccalls)
            and bool(calls)
            and timestamp(ccalls[0].get("started_at")) < timestamp(calls[0].get("started_at"))
            and timestamp(ccalls[-1].get("finished_at")) > timestamp(calls[-1].get("finished_at"))
        )
        control_samples = [
            o for o in observations if o.get("session_id") == control.get("session_id")
        ]
        checks["second_session_ledger_independent"] = (
            bool(control_samples)
            and integers(control_samples[0].get("session"), spent=0, inflight=0)
            and integers(control_samples[-1].get("session"), spent=200, inflight=0)
        )
    checks = {key: bool(value) for key, value in checks.items()}
    return {
        "result": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "evidence": {
            "scope": "dedicated byte-token backend; budget path only, not Qwen accounting",
            "agent": agent,
            "reserve_tokens": 300,
            "actual_tokens_per_call": 100,
            "tokens_per_day": 2000 if case == "day" else 2000000,
            "tokens_per_session": 200000 if case == "day" else 1000,
            "requests": links,
            "observation_count": len(observations),
        },
    }
