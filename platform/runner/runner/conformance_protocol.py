"""Opt-in T25: actual guest protocol faults, trusted recovery and memory evidence."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import uuid
from dataclasses import asdict

from temporalio.api.enums.v1 import EventType

from runner.conformance_admission import AdmissionSessions, KubernetesAdmission, ready, settled
from runner.activities import EXEC_DEADLINE
from runner.submit import submit
from runner.types import SessionInput

FAULTS = ("stdout_10mib", "invalid_json", "wrong_boundary", "done_true_rc0", "hang")
MAX_MEMORY_GROWTH = 64 << 20
MIN_MEMORY_HEADROOM = 64 << 20


def verdict(checks, evidence):
    return {
        "result": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "evidence": evidence,
    }


def evaluate_session(case, data):
    history = data.get("history", [])
    runs = [e for e in history if e["activity"] == "run_boundary"]
    creates = [e for e in history if e["activity"] == "create_sandbox"]
    deletes = [e for e in history if e["activity"] == "delete_sandbox"]
    fences = [e for e in history if e["activity"] == "set_generation"]
    stops = [e for e in history if e["activity"] == "stop_session"]
    snapshots = [e for e in history if e["activity"] == "snapshot_workspace"]
    healthy = case == "control"
    generations = [1] if healthy else [1, 2, 3]
    snapshot = data.get("snapshot")
    failed = [e for e in runs if e.get("failure_type")]
    completed = [e for e in runs if e.get("result") is not None]
    expected_message = {
        "control": "",
        "stdout_10mib": "truncated=True",
        "hang": "timed_out=True",
        "invalid_json": "malformed boundary output",
        "wrong_boundary": "inconsistent boundary output",
        "done_true_rc0": "inconsistent boundary output",
    }[case]
    delete_by_gen = {e["generation"]: e for e in deletes}
    create_by_gen = {e["generation"]: e for e in creates}
    checks = {
        "expected_outcome": data.get("outcome") == ("COMPLETED" if healthy else "FAILED"),
        "expected_generations": data.get("generation") == generations[-1],
        "one_committed_boundary": data.get("boundaries") == 1,
        "valid_checkpoint": isinstance(snapshot, str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", snapshot)),
        "only_boundary_zero_completed": len(completed) == 1
        and completed[0]["generation"] == 1
        and completed[0]["boundary"] == 0
        and completed[0]["result"].get("boundary") == 0
        and completed[0]["result"].get("done") is healthy,
        "exact_boundary_schedule": [(e["generation"], e["boundary"]) for e in runs]
        == ([(1, 0)] if healthy else [(1, 0), (1, 1), (2, 1), (3, 1)]),
        "expected_unknown_outcomes": len(failed) == (0 if healthy else 3)
        and all(
            e["failure_type"] == "OutcomeUnknown"
            and expected_message in (e.get("failure_message") or "")
            for e in failed
        ),
        "one_durable_checkpoint": len(snapshots) == 1
        and snapshots[0]["generation"] == 1
        and snapshots[0]["boundary"] == 0
        and snapshots[0].get("result") == snapshot,
        "checkpoint_after_result_before_next_boundary": len(snapshots) == len(completed) == 1
        and bool(completed[0].get("completed"))
        and bool(snapshots[0].get("completed"))
        and completed[0]["completed"] < snapshots[0]["scheduled"]
        and (healthy or (len(runs) > 1 and snapshots[0]["completed"] < runs[1]["scheduled"])),
        "restored_last_accepted_checkpoint": [(e["generation"], e.get("snapshot")) for e in creates]
        == [(g, None if g == 1 else snapshot) for g in generations],
        "all_deletions_confirmed": [e["generation"] for e in deletes] == generations
        and all(e.get("result") == "DELETED" for e in deletes),
        "deleted_before_replacement": all(
            (delete_by_gen.get(e["generation"] - 1, {}).get("completed") or 10**20) < e["scheduled"]
            for e in creates
            if e["generation"] > 1
        ),
        "fenced_after_deletion_before_create": [e["generation"] for e in fences] == generations
        and all(
            not e.get("failure_type")
            and bool(e.get("completed"))
            and e["completed"] < create_by_gen.get(e["generation"], {}).get("scheduled", 0)
            and (
                e["generation"] == 1
                or (delete_by_gen.get(e["generation"] - 1, {}).get("completed") or 10**20)
                < e["scheduled"]
            )
            for e in fences
        ),
        "session_stop_confirmed": len(stops) == 1 and stops[0].get("result") == "OK",
        "cleanup_complete": data.get("cleanup")
        == {"stop": "OK", "grant": "OK", "delete": "DELETED"}
        and data.get("status", {}).get("state") == "CLEANED",
        "no_session_pods": data.get("pods") == [],
    }
    return verdict(checks, data)


def evaluate_memory(sessions, records, inventories):
    samples = [e for e in records if e.get("event") == "boundary_memory"]
    samples.sort(key=lambda e: e.get("sampled_at", 0))
    expected = {
        (s["session_id"], e["generation"], e["boundary"])
        for s in sessions
        for e in s["history"]
        if e["activity"] == "run_boundary"
    }
    groups = {
        key: [
            e
            for e in samples
            if (e.get("session_id"), e.get("generation"), e.get("boundary")) == key
        ]
        for key in expected
    }
    sessions_set = {s["session_id"] for s in sessions}
    valid = bool(samples) and all(
        not e.get("error")
        and isinstance(e.get("process_id"), str)
        and e["process_id"]
        and all(
            type(e.get(k)) is int and e[k] > 0 for k in ("rss_bytes", "hwm_bytes", "limit_bytes")
        )
        and e["rss_bytes"] <= e["hwm_bytes"]
        for e in samples
    )
    baseline = samples[0] if valid else {}
    limits = {e["limit_bytes"] for e in samples} if valid else set()
    process_ids = {e["process_id"] for e in samples} if valid else set()
    max_rss = max(e["rss_bytes"] for e in samples) if valid else None
    max_hwm = max(e["hwm_bytes"] for e in samples) if valid else None
    workers = inventories[0] if inventories else []
    stable = bool(workers) and len(workers) == 1 and all(i == workers for i in inventories)
    checks = {
        "all_worker_logs_readable": not any(e.get("msg") == "log unavailable" for e in records),
        "memory_observations_present": valid,
        "all_boundary_start_end_samples": bool(expected)
        and all(
            [e["phase"] for e in group if e["phase"] != "periodic"] == ["start", "end"]
            for group in groups.values()
        ),
        "real_hang_deadline_observed": all(
            any(
                e.get("phase") == "end"
                and type(e.get("elapsed_ms")) in (int, float)
                and math.isfinite(e["elapsed_ms"])
                and e["elapsed_ms"] >= (EXEC_DEADLINE - 2) * 1000
                for e in groups.get((session["session_id"], generation, 1), [])
            )
            for session in sessions
            if session.get("case") == "hang"
            for generation in (1, 2, 3)
        ),
        "isolated_process_observations": bool(samples)
        and all(e.get("session_id") in sessions_set for e in samples),
        "one_worker_process": len(process_ids) == 1,
        "one_finite_memory_limit": len(limits) == 1,
        "same_ready_worker_no_restarts": stable
        and bool(inventories)
        and all(
            w.get("ready") is True
            and w.get("uid")
            and w.get("image_id")
            and type(w.get("restart_count")) is int
            for w in workers
        ),
        "process_is_observed_worker": valid
        and len(workers) == 1
        and all(e["process_id"].startswith(workers[0]["name"] + ":") for e in samples),
        "rss_growth_bounded": valid and max_rss - baseline["rss_bytes"] <= MAX_MEMORY_GROWTH,
        "hwm_growth_bounded": valid and max_hwm - baseline["hwm_bytes"] <= MAX_MEMORY_GROWTH,
        "configured_limit_headroom": valid
        and len(limits) == 1
        and max_hwm <= next(iter(limits)) - MIN_MEMORY_HEADROOM,
    }
    return verdict(
        checks,
        {
            "scope": "process-wide Linux RSS and kernel high-water; exclusive batch, same process",
            "growth_limit_bytes": MAX_MEMORY_GROWTH,
            "required_headroom_bytes": MIN_MEMORY_HEADROOM,
            "baseline_rss_bytes": baseline.get("rss_bytes"),
            "baseline_hwm_bytes": baseline.get("hwm_bytes"),
            "max_rss_bytes": max_rss,
            "max_hwm_bytes": max_hwm,
            "worker_inventories": inventories,
            "samples": samples,
        },
    )


async def decode_history(events, converter):
    """Decode actual scheduled inputs and terminal results using the tenant codec.

    Keep lifecycle evidence and fixed failure diagnostics, never the submitted
    task or guest stdout. Event ids establish ordering independently of log time.
    """
    from runner.conformance import failure_message

    scheduled = {}
    for event in events:
        kind = event.event_type
        if kind == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = event.activity_task_scheduled_event_attributes
            [arg] = await converter.decode(list(attrs.input.payloads), [dict])
            scheduled[event.event_id] = {
                "activity": attrs.activity_type.name,
                "scheduled": event.event_id,
                "completed": None,
                "generation": arg.get("generation"),
                "boundary": arg.get("boundary"),
                "snapshot": arg.get("restore"),
                "result": None,
                "failure_type": None,
                "failure_message": None,
            }
        elif kind == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            attrs = event.activity_task_completed_event_attributes
            row = scheduled[attrs.scheduled_event_id]
            values = await converter.decode(list(attrs.result.payloads))
            row.update(completed=event.event_id, result=values[0] if values else None)
        elif kind in (
            EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED,
            EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT,
        ):
            attrs = (
                event.activity_task_failed_event_attributes
                if kind == EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED
                else event.activity_task_timed_out_event_attributes
            )
            row = scheduled[attrs.scheduled_event_id]
            row.update(
                completed=event.event_id,
                failure_type=attrs.failure.application_failure_info.type
                if kind == EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED
                else "TimedOut",
                failure_message=await failure_message(attrs.failure, converter),
            )
    return list(scheduled.values())


class ProtocolSessions:
    def __init__(self, client, ops, agent, release, *, timeout=1800):
        if timeout < 1800:
            raise ValueError("T25 requires a session timeout of at least 1800 seconds")
        self.client, self.ops, self.agent, self.release = client, ops, agent, release
        self.timeout = timeout
        self.cleanup = AdmissionSessions(client, ops, timeout=timeout)

    async def run(self, case):
        inp = SessionInput(
            self.ops.tenant,
            self.agent,
            self.release,
            str(uuid.uuid4()),
            None,
            {"protocol_case": case},
            max_boundaries=2,
        )
        handle = await submit(self.client, inp)
        task = asyncio.create_task(handle.result())
        try:
            result = await asyncio.wait_for(asyncio.shield(task), self.timeout)
            events = [e async for e in handle.fetch_history_events()]
            history = await decode_history(events, self.client.data_converter)
            status = await handle.query("status")
            pods = await asyncio.to_thread(self.ops.pods, inp.session_id)
            return {
                "session_id": inp.session_id,
                "case": case,
                **asdict(result),
                "status": status,
                "history": history,
                "pods": [pod["metadata"]["name"] for pod in pods],
            }
        finally:
            await settled(self.cleanup._close(inp, handle, task))


class KubernetesProtocol(KubernetesAdmission):
    def workers(self):
        pods = self.core.list_namespaced_pod(
            "agent-platform",
            label_selector="app.kubernetes.io/name=session-runner,"
            + "agentplatform.io/tenant="
            + self.tenant,
            _request_timeout=15,
        ).items
        result = []
        for pod in pods:
            containers = [c for c in pod.status.container_statuses or [] if c.name == "runner"]
            container = containers[0] if len(containers) == 1 else None
            result.append(
                {
                    "name": pod.metadata.name,
                    "uid": pod.metadata.uid,
                    "restart_count": container.restart_count if container else None,
                    "image_id": container.image_id if container else None,
                    "ready": bool(
                        container
                        and container.ready
                        and pod.status.phase == "Running"
                        and not pod.metadata.deletion_timestamp
                    ),
                }
            )
        return sorted(result, key=lambda row: row["name"])


class ProtocolHarness:
    def __init__(self, ops, sessions, worker_logs, agent, release):
        self.ops, self.sessions, self.worker_logs = ops, sessions, worker_logs
        self.agent, self.release = agent, release

    async def t25(self):
        started = time.time()
        cases, data, inventories = [], [], []
        try:
            release = await asyncio.to_thread(self.ops.release, self.release)
            if release["spec"]["agent"] != self.agent or not ready(release):
                raise ValueError(
                    "T25 requires a signed, approved, Ready, drift-free protocol fixture"
                )
            inventories.append(await asyncio.to_thread(self.ops.workers))
            for case in ["control", *[c for fault in FAULTS for c in (fault, "control")]]:
                evidence = await self.sessions.run(case)
                data.append(evidence)
                result = {"case": case, **evaluate_session(case, evidence)}
                cases.append(result)
                inventories.append(await asyncio.to_thread(self.ops.workers))
                # If a valid session no longer works, stop submitting more faults.
                if case == "control" and result["result"] != "pass":
                    break
            raw = await self.worker_logs(max(1, time.time() - started + 5))
            records = []
            for line in raw.splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("msg") == "log unavailable" or (
                    record.get("event") == "boundary_memory"
                    and record.get("sampled_at", 0) >= started
                ):
                    records.append(record)
            memory = evaluate_memory(data, records, inventories)
            checks = {
                "all_eleven_sessions_recorded": len(cases) == 11,
                "all_session_checks": all(c["result"] == "pass" for c in cases),
                "worker_memory_and_lifecycle": memory["result"] == "pass",
            }
            return {
                "id": "T25",
                "name": "protocol robustness",
                "cases": cases,
                **verdict(
                    checks,
                    {
                        "fixture_agent": self.agent,
                        "fixture_release": self.release,
                        "memory": memory,
                    },
                ),
            }
        except Exception as exc:
            return {
                "id": "T25",
                "result": "error",
                "cases": cases,
                "error": type(exc).__name__,
                "detail": str(exc)[:1024],
            }
