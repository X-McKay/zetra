"""T17 evidence evaluation. No status is inferred from a timeout or missing record."""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from collections import Counter
from pathlib import Path

N = 1_000_000_000
PERIOD = N // 5
SHA = re.compile(r"[0-9a-f]{64}\Z")


def read_evidence(root, digest):
    if not isinstance(digest, str) or not SHA.fullmatch(digest):
        raise ValueError("invalid accepted snapshot digest")
    with (Path(root) / digest[:2] / digest).open("rb") as source:
        hashed, size = hashlib.sha256(), 0
        while chunk := source.read(1 << 20):
            size += len(chunk)
            if size > 64 << 20:
                raise ValueError("snapshot too large")
            hashed.update(chunk)
        if hashed.hexdigest() != digest:
            raise ValueError("snapshot digest mismatch")
        source.seek(0)
        with tarfile.open(fileobj=source, mode="r:") as archive:
            members = [m for m in archive if m.name == "workspace/t17/requests.json"]
            if len(members) != 1 or not members[0].isfile() or members[0].size > 1 << 20:
                raise ValueError("invalid T17 evidence member")
            result = json.load(archive.extractfile(members[0]))
    if not isinstance(result, dict):
        raise ValueError("invalid T17 evidence")
    return result


def restored(o):
    pods, endpoints = o.get("pods", []), o.get("endpoints", [])
    return (
        o.get("replicas") == o.get("ready_replicas") == 2
        and o.get("generation") == o.get("observed_generation")
        and len(pods) == len(endpoints) == 2
        and len({p.get("uid") for p in pods}) == 2
        and all(p.get("uid") and p.get("ready") is True and p.get("image_id") for p in pods)
        and all(e.get("ready") is True for e in endpoints)
        and {e.get("uid") for e in endpoints} == {p.get("uid") for p in pods}
    )


def empty(o):
    return (
        o.get("replicas") == o.get("ready_replicas") == 0
        and o.get("generation") == o.get("observed_generation")
        and o.get("pods") == o.get("endpoints") == []
    )


def evaluate(*, probe, marker, duration, receipt, observations, gateway, logs_readable, session):
    rows = probe.get("requests", [])
    down, up = receipt.get("down", {}), receipt.get("up", {})
    dn, da = down.get("started_ns", 0), down.get("acknowledged_ns", 0)
    un, ua = up.get("started_ns", 0), up.get("acknowledged_ns", 0)
    origin = probe.get("origin_ns", 0)
    first, last = (observations[0], observations[-1]) if observations else ({}, {})
    initial_ids = {p.get("uid") for p in first.get("pods", [])}
    final_ids = {p.get("uid") for p in last.get("pods", [])}
    starts = [r.get("started_ns", 0) for r in rows]
    offsets = [r.get("offset_ns", -1) for r in rows]
    sample_times = [o.get("observed_ns", 0) for o in observations]
    checks = {
        "completed_generation_one_cleanup": session.get("outcome") == "COMPLETED"
        and session.get("generation") == 1
        and session.get("cleanup") == {"stop": "OK", "delete": "DELETED", "grant": "OK"},
        "explicit_operator_receipts": set(receipt)
        == {"schema", "marker", "deployment_uid", "original_replicas", "down", "up"}
        and receipt.get("schema") == "t17-v1"
        and receipt.get("marker") == marker
        and receipt.get("deployment_uid") == first.get("uid")
        and bool(first.get("uid"))
        and receipt.get("original_replicas") == 2
        and 0 < dn <= da < un <= ua
        and bool(down.get("resource_version"))
        and bool(up.get("resource_version"))
        and down.get("resource_version") != up.get("resource_version")
        and down.get("generation", 0) == first.get("generation", 0) + 1
        and up.get("generation", 0) == down.get("generation", 0) + 1,
        "exact_probe_and_count": probe.get("schema") == "t17-v1"
        and probe.get("marker") == marker
        and probe.get("duration_seconds") == duration
        and 60 <= duration <= 240
        and probe.get("rate") == 5
        and len(rows) == duration * 5
        and [r.get("sequence") for r in rows] == list(range(1, duration * 5 + 1))
        and [r.get("trace_id") for r in rows]
        == [marker + f"{i:016x}" for i in range(1, duration * 5 + 1)],
        "five_hz_no_gaps": bool(rows)
        and all(0 <= offset - i * PERIOD <= 50_000_000 for i, offset in enumerate(offsets))
        and all(0 < b - a <= 300_000_000 for a, b in zip(starts, starts[1:])),
        "consistent_probe_clock": bool(rows)
        and all(
            abs(r.get("started_ns", 0) - origin - r.get("offset_ns", 0)) <= 100_000_000
            for r in rows
        ),
        "one_token_outlives_probe": probe.get("token_expires_ns", 0)
        >= origin + (duration + 15) * N,
        "every_request_has_real_response": bool(rows)
        and all(
            r.get("status") in (200, 403)
            and r.get("error") is None
            and 0 <= r.get("finished_ns", 0) - r.get("started_ns", 0) <= 4 * N
            for r in rows
        ),
        "deployment_identity_and_configuration_unchanged": bool(observations)
        and isinstance(first.get("spec_sha"), str)
        and bool(SHA.fullmatch(first["spec_sha"]))
        and all(
            o.get("uid") == first.get("uid") and o.get("spec_sha") == first.get("spec_sha")
            for o in observations
        ),
        "continuous_kubernetes_observation": len(observations) > 2
        and all(0 <= o.get("observed_ns", 0) - o.get("started_ns", 0) <= N for o in observations)
        and all(0 < b - a <= 1_500_000_000 for a, b in zip(sample_times, sample_times[1:]))
        and bool(rows)
        and sample_times[0] < starts[0]
        and sample_times[-1] >= max(r.get("finished_ns", 0) for r in rows),
        "two_original_ready_replicas": restored(first),
        "two_new_ready_replicas_restored": restored(last)
        and len(final_ids) == 2
        and not initial_ids & final_ids
        and last.get("generation") == up.get("generation")
        and {p.get("image_id") for p in first.get("pods", [])}
        == {p.get("image_id") for p in last.get("pods", [])},
        "all_gateway_logs_readable": logs_readable is True,
    }
    phase_consistent = bool(observations)
    for observation in observations:
        if observation.get("started_ns", 0) >= ua:
            replicas, generation = 2, up.get("generation")
        elif da <= observation.get("started_ns", 0) and observation.get("observed_ns", 0) < un:
            replicas, generation = 0, down.get("generation")
        elif observation.get("observed_ns", 0) < dn:
            replicas, generation = 2, first.get("generation")
        else:
            continue  # a read overlapping a recorded mutation can see either version
        phase_consistent &= (
            observation.get("replicas") == replicas and observation.get("generation") == generation
        )
    checks["only_recorded_replica_changes"] = phase_consistent
    relevant = [g for g in gateway if str(g.get("trace.id", "")).startswith(marker)]
    counts = Counter(g.get("trace.id") for g in relevant)
    by_trace = {g.get("trace.id"): g for g in relevant}
    checks["one_exact_gateway_response_per_request"] = (
        len(relevant) == len(rows)
        and bool(rows)
        and all(
            counts[r.get("trace_id")] == 1
            and by_trace[r["trace_id"]].get("http.status") == str(r.get("status"))
            and by_trace[r["trace_id"]].get("http.path") == "/kubernetes/mcp"
            and by_trace[r["trace_id"]].get("http.method") == "POST"
            for r in rows
        )
    )
    baseline = [r for r in rows if r.get("finished_ns", 0) < dn]
    denied = [r for r in rows if dn + N <= r.get("started_ns", 0) < un]
    zero = [
        o
        for o in observations
        if da <= o.get("started_ns", 0) and o.get("observed_ns", 0) < un and empty(o)
    ]
    zero_start = zero[0]["observed_ns"] if zero else un
    zero_window = [o for o in observations if zero_start <= o.get("observed_ns", 0) < un]
    checks["five_seconds_all_allow_before_scale_down"] = (
        len(baseline) >= 25
        and baseline[-1]["started_ns"] - baseline[0]["started_ns"] >= 4.8 * N
        and all(r.get("status") == 200 for r in baseline)
    )
    checks["every_request_after_first_second_is_403"] = len(denied) >= 50 and all(
        r.get("status") == 403 for r in denied
    )
    checks["ten_seconds_observed_zero_pods_and_endpoints"] = (
        len(zero_window) >= 10
        and zero_window[-1]["observed_ns"] - zero_start >= 10 * N
        and all(empty(o) and o.get("generation") == down.get("generation") for o in zero_window)
        and len(
            [r for r in denied if zero_start <= r["started_ns"] < zero_window[-1]["started_ns"]]
        )
        >= 50
    )
    recovery = [o for o in observations if o.get("started_ns", 0) >= ua and restored(o)]
    recovered_at = recovery[0]["observed_ns"] if recovery else 0
    recovered = [r for r in rows if recovered_at and r.get("started_ns", 0) >= recovered_at + N]
    # One second permits the observed endpoints to propagate; evidence preserves every
    # transition response, and the five-second recovered segment must be entirely 200.
    checks["five_seconds_all_allow_after_restore"] = (
        len(recovered) >= 25
        and recovered[-1]["started_ns"] - recovered[0]["started_ns"] >= 4.8 * N
        and all(r.get("status") == 200 for r in recovered)
    )
    return {
        "result": "pass" if all(checks.values()) else "fail",
        "checks": {k: bool(v) for k, v in checks.items()},
        "evidence": {
            "marker": marker,
            "operator_receipt": receipt,
            "observations": observations,
            "requests": rows,
            "gateway": relevant,
            "eligible_denials": len(denied),
            "clock_scope": "operator, Kubernetes observer and sandbox wall clocks must be synchronized; probe monotonic drift is checked",
        },
    }
