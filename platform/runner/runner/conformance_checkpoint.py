"""T23 evidence: actual histories, completed MCP reads and durable checkpoint bytes."""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from collections import Counter
from pathlib import Path

from runner.activities import pod_name
from runner.conformance_checkpoint_worker import STAGES

SHA = re.compile(r"[0-9a-f]{64}\Z")


def checkpoint_state(root, key):
    if not isinstance(key, str) or not SHA.fullmatch(key):
        raise ValueError("invalid checkpoint key")
    with (Path(root) / key[:2] / key).open("rb") as source:
        digest, size = hashlib.sha256(), 0
        while chunk := source.read(1 << 20):
            size += len(chunk)
            if size > 64 << 20:
                raise ValueError("checkpoint exceeds bound")
            digest.update(chunk)
        if digest.hexdigest() != key:
            raise ValueError("checkpoint hash mismatch")
        source.seek(0)
        with tarfile.open(fileobj=source, mode="r:") as archive:
            members = archive.getmembers()

            def read(name):
                matches = [m for m in members if m.name == name]
                if len(matches) != 1 or not matches[0].isfile() or matches[0].size > 4096:
                    raise ValueError("checkpoint metadata member is not one bounded regular file")
                return json.load(archive.extractfile(matches[0]))

            marker, state = read("workspace/checkpoint.json"), read("state/state.json")
            boundary = marker.get("last_boundary")
            done = [m for m in members if m.name == f"state/{boundary}.done"]
            if len(done) != 1 or not done[0].isfile() or not 0 < done[0].size < 128:
                raise ValueError("checkpoint lacks completed boundary marker")
    return {"marker": marker, "next": state.get("next"), "bytes": size, "done": True}


def read_evidence(session, aps, gateway, captures):
    selected_aps = [
        d for d in aps if d.get("session_id") == session and ("decision_id" in d or d.get("route"))
    ]
    selected_gateway = [g for g in gateway if g.get("agent.session_id") == session]
    selected_captures = [
        c for c in captures if c.get("x_agent", {}).get("x-agent-session-id") == session
    ]
    ids = {
        "aps": [d.get("decision_id") for d in selected_aps],
        "gateway": [g.get("agent.decision_id") for g in selected_gateway],
        "capture": [c.get("x_agent", {}).get("x-agent-decision-id") for c in selected_captures],
    }
    complete = (
        all(
            all(isinstance(key, str) and key for key in values) and len(values) == len(set(values))
            for values in ids.values()
        )
        and set(ids["aps"]) == set(ids["gateway"]) == set(ids["capture"])
        and all(d.get("route") == "mcp" and d.get("backend") == "kubernetes" for d in selected_aps)
        and all(g.get("http.path") == "/kubernetes/mcp" for g in selected_gateway)
    )
    decisions = [d for d in selected_aps if d.get("method") == "tools/call"]
    records = []
    for d in decisions:
        key = d.get("decision_id")
        gw = [g for g in gateway if g.get("agent.decision_id") == key]
        cp = [c for c in captures if c.get("x_agent", {}).get("x-agent-decision-id") == key]
        headers = cp[0].get("x_agent", {}) if len(cp) == 1 else {}
        records.append(
            {k: d.get(k) for k in ("decision_id", "generation", "pod", "tool", "reason")}
        )
        records[-1].update(
            gateway_count=len(gw),
            capture_count=len(cp),
            gateway_status=gw[0].get("http.status") if len(gw) == 1 else None,
            capture_status=cp[0].get("status") if len(cp) == 1 else None,
            capture_bound=(
                headers.get("x-agent-session-id") == session
                and headers.get("x-agent-id") == "tenant-test/conformance-checkpoint"
                and headers.get("x-agent-generation") == str(d.get("generation"))
            ),
        )
    return {
        "reads": records,
        "read_coverage": {
            "complete": complete,
            "ids": ids,
            "control_methods": dict(
                Counter(
                    str(d.get("method")) for d in selected_aps if d.get("method") != "tools/call"
                )
            ),
        },
    }


def evaluate_case(case, data):
    healthy = case.startswith("control-")
    expected_generation = 1 if healthy else 2
    history = data.get("history", [])
    runs = [h for h in history if h["activity"] == "run_boundary"]
    snapshots = [h for h in history if h["activity"] == "snapshot_workspace"]
    committed = [h for h in snapshots if isinstance(h.get("result"), str)]
    creates = [h for h in history if h["activity"] == "create_sandbox"]
    waits = [h for h in history if h["activity"] == "wait_ready"]
    fences = [h for h in history if h["activity"] == "set_generation"]
    deletes = [h for h in history if h["activity"] == "delete_sandbox"]
    expected_runs = (
        [(1, 0), (1, 1), (1, 2)]
        if healthy
        else ([(1, 0), (1, 1), (1, 2), (2, 2)] if case == "e" else [(1, 0), (1, 1), (2, 1), (2, 2)])
    )
    expected_snapshots = (
        [(1, 0), (1, 1), (1, 2)]
        if healthy
        else ([(1, 0), (1, 1), (2, 2)] if case == "e" else [(1, 0), (2, 1), (2, 2)])
    )
    expected_reads = [1] if healthy or case == "e" else ([2] if case == "a" else [1, 2])
    reads = data.get("reads", [])
    stages = data.get("stages", [])
    read_stages = [s for s in stages if s.get("stage") == "read_complete"]
    keys = [s["result"] for s in committed]
    states = data.get("checkpoints", {})
    checks = {
        "completed_three_boundaries": data.get("outcome") == "COMPLETED"
        and data.get("boundaries") == 3
        and data.get("generation") == expected_generation,
        "confirmed_cleanup": data.get("cleanup")
        == {"stop": "OK", "delete": "DELETED", "grant": "OK"}
        and data.get("pods") == [],
        "exact_boundary_attempts": [(h["generation"], h["boundary"]) for h in runs]
        == expected_runs,
        "one_checkpoint_per_committed_boundary": [
            (h["generation"], h["boundary"]) for h in committed
        ]
        == expected_snapshots
        and len(set(keys)) == 3
        and data.get("snapshot") == keys[-1],
        "actual_hash_verified_checkpoints": len(states) == 3
        and all(
            states.get(h["result"], {}).get("marker")
            == {
                "run_id": data.get("run_id"),
                "session_id": data.get("session_id"),
                "last_boundary": h["boundary"],
                "generation": h["generation"],
            }
            and states[h["result"]].get("next") == h["boundary"] + 1
            and states[h["result"]].get("done") is True
            for h in committed
        ),
        "exact_completed_read_counts": sorted(r.get("generation") for r in reads) == expected_reads
        and sorted(r.get("generation") for r in read_stages) == expected_reads
        and len({r.get("decision_id") for r in reads}) == len(expected_reads),
        "real_mcp_gateway_and_capture_success": bool(reads)
        and all(
            r.get("tool") == "pods_list_in_namespace"
            and r.get("reason") == "allow"
            and r.get("pod") == pod_name(data["session_id"], r.get("generation"))
            and r.get("gateway_count") == r.get("capture_count") == 1
            and str(r.get("gateway_status")) == "200"
            and r.get("capture_status") == 200
            and r.get("capture_bound") is True
            for r in reads
        ),
        "checkpoint_after_real_boundary_result": len(committed) == 3
        and all(
            len(
                matches := [
                    r
                    for r in runs
                    if r["generation"] == h["generation"]
                    and r["boundary"] == h["boundary"]
                    and isinstance(r.get("result"), dict)
                    and r["result"].get("boundary") == h["boundary"]
                    and r["result"].get("done") is (h["boundary"] == 2)
                ]
            )
            == 1
            and matches[0]["completed"] < h["scheduled"] < h["completed"]
            for h in committed
        ),
        "all_log_sources_readable": data.get("logs_readable") is True,
        "complete_transport_correlation": data.get("read_coverage", {}).get("complete") is True,
    }
    expected_stages = []
    for row in runs:
        if isinstance(row.get("result"), dict) or (
            case == "b" and row["generation"] == 1 and row["boundary"] == 1
        ):
            expected_stages.append(("boundary_start", row["generation"], row["boundary"]))
            if row["boundary"] == 1:
                expected_stages.append(("read_complete", row["generation"], row["boundary"]))
            if isinstance(row.get("result"), dict):
                expected_stages.append(("done", row["generation"], row["boundary"]))
    checks["bound_observed_stage_order"] = (
        all(
            stage.get("session_id") == data.get("session_id")
            and stage.get("run_id") == data.get("run_id")
            and stage.get("case") == ("control" if healthy else case)
            for stage in stages
        )
        and [(s.get("stage"), s.get("generation"), s.get("boundary")) for s in stages]
        == expected_stages
    )
    restore = None if healthy or not keys else keys[1] if case == "e" else keys[0]
    expected_restore = [(1, None)] if healthy else [(1, None), (2, restore)]
    checks["restored_last_accepted_checkpoint"] = all(
        [(h["generation"], h.get("snapshot")) for h in group] == expected_restore
        and all(h.get("completed") and not h.get("failure_type") for h in group)
        for group in (creates, waits)
    )
    checks["confirmed_deletion_before_replacement"] = healthy or (
        len(old := [h for h in deletes if h["generation"] == 1 and h.get("result") == "DELETED"])
        == 1
        and len(new := [h for h in fences if h["generation"] == 2]) == 1
        and len(creates) == 2
        and old[0]["completed"] < new[0]["scheduled"]
        and new[0]["completed"] < creates[1]["scheduled"]
    )
    if not healthy:
        receipt = data.get("receipt") or {}
        gate_history = data.get("gate_history", [])
        point, boundary = STAGES[case]
        checks["exact_observed_kill_receipt"] = (
            receipt.get("deleted") is True
            and receipt.get("stage") == point
            and receipt.get("case") == case
            and receipt.get("boundary") == boundary
            and receipt.get("generation") == 1
            and receipt.get("session_id") == data.get("session_id")
            and receipt.get("run_id") == data.get("run_id")
            and bool(receipt.get("pod_uid"))
            and receipt.get("pod") == pod_name(data["session_id"], 1)
            and receipt.get("reached_at", "")
            <= receipt.get("delete_started_at", "")
            <= receipt.get("deleted_at", "")
        )
        gate_runs = [
            h
            for h in gate_history
            if h["activity"] == "run_boundary"
            and h["generation"] == 1
            and h["boundary"] == boundary
        ]
        gate_snap = [
            h
            for h in gate_history
            if h["activity"] == "snapshot_workspace" and h["generation"] == 1 and h["boundary"] == 1
        ]
        checks["actual_activity_at_failure_point"] = len(gate_runs) == 1 and (
            (case in ("a", "b", "e") and gate_runs[0].get("completed") is None)
            or (
                case in ("c", "d")
                and isinstance(gate_runs[0].get("result"), dict)
                and len(gate_snap) == 1
                and gate_snap[0].get("completed") is None
            )
        )
        checks["after_commit_does_not_repeat_read"] = case != "e" or (
            len(gate_snap) == 1
            and gate_snap[0].get("result") == restore
            and gate_snap[0]["completed"] < gate_runs[0]["scheduled"]
        )
        failed = [h for h in history if h.get("failure_type")]
        checks["one_real_failed_attempt"] = len(failed) == 1 and failed[0]["generation"] == 1
        checks["upload_failure_is_uncommitted"] = (
            case != "d"
            or (
                failed[0].get("activity") == "snapshot_workspace"
                and failed[0].get("failure_type") == "SnapshotFailed"
            )
            if failed
            else case != "d"
        )
    if case == "d":
        upload = (data.get("receipt") or {}).get("upload") or {}
        candidate = data.get("candidate") or {}
        checks["observed_partial_copy_candidate_not_accepted"] = (
            type(upload.get("copied_bytes")) is int
            and type(candidate.get("bytes")) is int
            and 0 < upload["copied_bytes"] < candidate["bytes"]
            and isinstance(upload.get("candidate_key"), str)
            and bool(SHA.fullmatch(upload["candidate_key"]))
            and upload["candidate_key"] not in keys
            and candidate.get("marker")
            == {
                "run_id": data.get("run_id"),
                "session_id": data.get("session_id"),
                "last_boundary": 1,
                "generation": 1,
            }
            and candidate.get("next") == 2
            and candidate.get("done") is True
        )
    return {
        "case": case,
        "result": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "evidence": data,
        "completed_read_count_by_generation": dict(Counter(r.get("generation") for r in reads)),
    }
