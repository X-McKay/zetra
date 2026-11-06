"""Read-only T24 orchestration; root owns every Kubernetes fault and restoration."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from datetime import datetime

from kubernetes.client.exceptions import ApiException
from temporalio.api.enums.v1 import EventType
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError

from runner.aps import APS
from runner.conformance_admission import AdmissionSessions, ready, settled
from runner.conformance_fail_closed_runtime import OutageOps
from runner.conformance_lifecycle import FINALIZER, aps_ready, clean, evaluate_case, held, result
from runner.conformance_lifecycle_control import CASES, FAULTS
from runner.types import SessionInput
from runner.workflow import EXECUTION_TIMEOUT, SessionWorkflow


def validate_grant(value, session):
    if (
        not isinstance(value, dict)
        or set(value) != {"session_id", "handle"}
        or value["session_id"] != session
        or not isinstance(value["handle"], str)
        or not re.fullmatch(r"gh_[A-Za-z0-9_-]{1,100}", value["handle"])
    ):
        raise ValueError("lifecycle requires an opaque grant bound to this exact session")
    return value["handle"]


def timestamp(value):
    return value.seconds + value.nanos / 1_000_000_000


async def decode_history(events, converter, session):
    """Trusted payload decoding; only fixed lifecycle metadata is retained."""
    rows, terminal, progress = {}, {}, {}
    if len(events) > 10000:
        raise ValueError("lifecycle history exceeds bound")
    for event in events:
        kind, at = event.event_type, timestamp(event.event_time)
        if kind == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = event.activity_task_scheduled_event_attributes
            name = attrs.activity_type.name
            if name not in {
                "preflight",
                "set_generation",
                "create_sandbox",
                "wait_ready",
                "run_boundary",
                "snapshot_workspace",
                "delete_sandbox",
                "stop_session",
                "delete_grant",
                "close_session",
                "delete_orphan",
            }:
                continue
            [arg] = await converter.decode(list(attrs.input.payloads), [dict])
            nested = arg.get("inp", arg.get("session", arg.get("pod", arg)))
            if not isinstance(nested, dict) or nested.get("session_id") != session:
                continue
            owner = nested if name not in {"close_session", "delete_orphan"} else arg
            if (
                owner.get("tenant") != "tenant-test"
                or nested.get("agent") != "conformance-lifecycle"
            ):
                raise ValueError("lifecycle history owner mismatch")
            rows[event.event_id] = {
                "activity": name,
                "scheduled_id": event.event_id,
                "scheduled_at": at,
                "completed_at": None,
                "result": None,
                "failure_type": None,
                "attempt": None,
                "generation": arg.get("generation", nested.get("generation")),
                "boundary": arg.get("boundary"),
                "restore": arg.get("restore")
                if isinstance(arg.get("restore"), str)
                and re.fullmatch(r"[0-9a-f]{64}", arg["restore"])
                else None,
            }
        elif kind == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            attrs = event.activity_task_started_event_attributes
            if attrs.scheduled_event_id in rows:
                rows[attrs.scheduled_event_id].update(attempt=attrs.attempt, started_at=at)
        elif kind == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            attrs = event.activity_task_completed_event_attributes
            row = rows.get(attrs.scheduled_event_id)
            if row is None:
                continue
            values = await converter.decode(list(attrs.result.payloads))
            value = values[0] if values else None
            name = row["activity"]
            if name == "run_boundary" and isinstance(value, dict):
                value = {k: value.get(k) for k in ("boundary", "done")}
            elif name == "snapshot_workspace":
                if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                    raise ValueError("invalid lifecycle snapshot receipt")
            elif name == "create_sandbox":
                if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9-]{1,253}", value):
                    raise ValueError("invalid lifecycle Pod receipt")
            elif name in {
                "delete_sandbox",
                "delete_orphan",
                "stop_session",
                "delete_grant",
                "close_session",
            }:
                if value not in ("DELETED", "NOT_CONFIRMED", "OK"):
                    raise ValueError("invalid lifecycle cleanup receipt")
            else:
                value = None
            row.update(completed_at=at, result=value)
        elif kind in (
            EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED,
            EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT,
        ):
            failed = kind == EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED
            attrs = (
                event.activity_task_failed_event_attributes
                if failed
                else event.activity_task_timed_out_event_attributes
            )
            row = rows.get(attrs.scheduled_event_id)
            if row is not None:
                failure_type = attrs.failure.application_failure_info.type if failed else "TimedOut"
                if failure_type not in {
                    "APSUnavailable",
                    "OutcomeUnknown",
                    "SandboxLost",
                    "ExecFailed",
                    "SnapshotFailed",
                    "TimedOut",
                    "CleanupFailed",
                    "Forbidden",
                    "DeletionNotConfirmed",
                }:
                    failure_type = "OtherFailure"
                row.update(completed_at=at, failure_type=failure_type)
        elif kind in (
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED,
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED,
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED,
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT,
        ):
            terminal = {
                "kind": EventType.Name(kind).removeprefix("EVENT_TYPE_WORKFLOW_EXECUTION_"),
                "at": at,
            }
            if kind == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED:
                values = await converter.decode(
                    list(event.workflow_execution_completed_event_attributes.result.payloads)
                )
                if values and isinstance(values[0], dict):
                    progress = {
                        k: values[0].get(k) for k in ("outcome", "generation", "boundaries")
                    }
    return {"history": list(rows.values()), "terminal": terminal, "progress": progress}


class LifecycleObserver:
    def __init__(self):
        self.aps = APS.from_env()
        self.aps.timeout = 2

    def read(self, session):
        raw = self.aps.get("/v1/conformance/sessions/" + session + "?agent=conformance-lifecycle")
        expected = {
            "session_id",
            "tenant",
            "agent",
            "observed_at",
            "generation",
            "generation_present",
            "stopped",
            "cleanup_pending",
            "grant_present",
            "vault_present",
            "grant_ttl_seconds",
            "vault_ttl_seconds",
            "refresh_backed",
            "access_current",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ValueError("invalid lifecycle observer schema")
        parsed = datetime.fromisoformat(raw["observed_at"].replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("lifecycle observation must have a timezone")
        raw["observed_at"] = parsed.timestamp()
        return raw


class LifecycleOps(OutageOps):
    def __init__(self, core, custom, apps, control):
        super().__init__(core, custom, apps)
        self.control = control

    def sandbox_rows(self, session):
        return [
            {
                "name": p["metadata"]["name"],
                "uid": p["metadata"]["uid"],
                "generation": int(p["metadata"]["labels"]["agentplatform.io/generation"]),
                "deleting": bool(p["metadata"].get("deletionTimestamp")),
                "finalizers": p["metadata"].get("finalizers", []),
            }
            for p in self.pods(session)
        ]

    def workers(self):
        pods = self.core.list_namespaced_pod(
            "agent-platform",
            label_selector="agentplatform.io/lifecycle-run=" + self.control.config["run_id"],
            _request_timeout=5,
        ).items
        return [
            {
                "name": p.metadata.name,
                "uid": p.metadata.uid,
                "image": p.spec.containers[0].image,
                "owner_uid": next(
                    (
                        o.uid
                        for o in (p.metadata.owner_references or [])
                        if o.controller is True and o.kind == "ReplicaSet"
                    ),
                    None,
                ),
                "ready": p.status.phase == "Running"
                and not p.metadata.deletion_timestamp
                and bool(p.status.container_statuses)
                and all(c.ready for c in p.status.container_statuses),
            }
            for p in pods
        ]

    def absent(self, namespace, pod):
        try:
            self.core.read_namespaced_pod(pod["name"], namespace, _request_timeout=5)
        except ApiException as exc:
            if exc.status == 404:
                return {"name": pod["name"], "uid": pod.get("uid"), "http_status": 404}
            raise
        return {"name": pod["name"], "uid": pod.get("uid"), "http_status": 200}


async def gates(handle, converter, session):
    desc = await handle.describe()
    rows = []
    for pending in desc.raw_description.pending_activities:
        payloads = list(pending.heartbeat_details.payloads)
        if not payloads:
            continue
        values = await converter.decode(payloads)
        if (
            len(values) != 1
            or not isinstance(values[0], dict)
            or values[0].get("schema") != "t24-gate-v1"
        ):
            continue  # ordinary runner heartbeats are not lifecycle gates
        value = values[0]
        if (
            set(value) != {"schema", "case", "session_id", "stage", "generation", "worker_uid"}
            or value["session_id"] != session
            or value["case"] not in CASES
            or value["stage"] not in {"boundary1", "delete_sandbox", "delete_grant"}
            or type(value["generation"]) is not int
            or value["generation"] != 1
        ):
            raise ValueError("invalid lifecycle heartbeat binding")
        rows.append({**value, "observed_at": time.time()})
    return rows


def safe_receipt(value, case, session):
    if value is None:
        return {}
    required = {"session_id", "fault", "target_uid", "started_at"}
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - {"restored_at"}
        or value["session_id"] != session
        or value["fault"] != case
        or not isinstance(value["target_uid"], str)
        or not re.fullmatch(r"[a-zA-Z0-9-]{1,100}", value["target_uid"])
        or any(
            type(value[k]) not in (int, float) or not 0 < value[k] < 10**11
            for k in ("started_at", *(["restored_at"] if "restored_at" in value else []))
        )
    ):
        raise ValueError("invalid lifecycle operator receipt")
    return value


class LifecycleHarness:
    def __init__(self, client, ops, control, observer, release):
        if release != control.config["release"]:
            raise ValueError("lifecycle release differs from operator-bound configuration")
        self.client, self.ops, self.control, self.observer = client, ops, control, observer
        self.cfg = control.config
        self.cleanup = AdmissionSessions(client, ops, timeout=180)
        self.notified = set()

    def notify(self, case, phase, **metadata):
        key = (case, phase)
        if key not in self.notified:
            print(
                json.dumps(
                    {
                        "test": "T24",
                        "run_id": self.cfg["run_id"],
                        "case": case,
                        "session_id": self.cfg["sessions"][case],
                        "phase": phase,
                        **metadata,
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
            self.notified.add(key)

    async def schedule(self):
        from temporalio.client import ScheduleOverlapPolicy

        desc = await self.client.get_schedule_handle("cleanup-reconciler-tenant-test").describe()
        schedule = desc.schedule
        if (
            schedule.state.paused
            or schedule.action.workflow != "CleanupReconcilerWorkflow"
            or schedule.action.task_queue != "cleanup-tenant-test"
            or schedule.policy.overlap != ScheduleOverlapPolicy.SKIP
            or len(schedule.spec.intervals) != 1
            or schedule.spec.intervals[0].every.total_seconds() != 300
        ):
            raise ValueError("T24 requires the ordinary active five-minute reconciler Schedule")
        return desc

    async def reconciler(self, baseline, session):
        current = await self.schedule()
        if (
            current.schedule != baseline.schedule
            or current.info.last_updated_at != baseline.info.last_updated_at
        ):
            raise ValueError("reconciler Schedule changed during lifecycle case")
        expected = set(baseline.info.next_action_times)
        rows = []
        for action in current.info.recent_actions:
            if action.scheduled_at not in expected:
                continue
            handle = self.client.get_workflow_handle(
                action.action.workflow_id, run_id=action.action.first_execution_run_id
            )
            events = [e async for e in handle.fetch_history_events()]
            evidence = await decode_history(events, self.client.data_converter, session)
            rows.extend(
                {
                    **row,
                    "scheduled_action": True,
                    "workflow_id": action.action.workflow_id,
                    "run_id": action.action.first_execution_run_id,
                }
                for row in evidence["history"]
            )
        return rows

    async def validate(self):
        release = await asyncio.to_thread(self.ops.release, self.cfg["release"])
        agent = await asyncio.to_thread(self.ops.object, "agents", self.cfg["agent"])
        capabilities = agent["spec"].get("capabilities", {})
        models = capabilities.get("models", [])
        if (
            release["spec"]["agent"] != self.cfg["agent"]
            or not ready(release)
            or not agent["metadata"].get("uid")
            or not agent["spec"].get("owner")
            or agent["spec"].get("identity") != {"mode": "agent"}
            or capabilities.get("mcp") != []
            or len(models) != 1
            or models[0].get("backend") != "llm-main"
            or models[0].get("models") != ["Qwen3.6-35B-A3B-NVFP4"]
            or models[0].get("budget") != {"tokensPerDay": 1, "tokensPerSession": 1}
        ):
            raise ValueError(
                "lifecycle requires the exact signed Ready fixture and narrow capabilities"
            )
        return {
            "uid": agent["metadata"]["uid"],
            "spec": agent["spec"],
            "release_uid": release["metadata"]["uid"],
        }

    async def grant(self, case):
        self.notify(case, "grant_required")
        until = time.monotonic() + 300
        while time.monotonic() < until:
            value = await asyncio.to_thread(self.control.get, "grant-" + case)
            if value is not None:
                return validate_grant(value, self.cfg["sessions"][case])
            await asyncio.sleep(0.2)
        raise TimeoutError("just-in-time lifecycle grant deadline")

    async def run_case(self, case):
        schedule = await self.schedule()
        grant = await self.grant(case)
        inp = SessionInput(
            self.cfg["tenant"],
            self.cfg["agent"],
            self.cfg["release"],
            self.cfg["sessions"][case],
            grant,
            {"lifecycle_case": case},
            max_boundaries=2,
        )
        inp.validate()
        handle = await self.client.start_workflow(
            SessionWorkflow.run,
            inp,
            id=inp.workflow_id,
            task_queue=self.cfg["queue"],
            execution_timeout=EXECUTION_TIMEOUT,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        task = asyncio.create_task(handle.result())
        data = {
            "case": case,
            "session_id": inp.session_id,
            "run_id": handle.first_execution_run_id,
            "worker_image": self.cfg["worker_image"],
            "samples": [],
            "history": [],
            "reconciler": [],
            "evidence_errors": [],
            "cleanup_gates": [],
            "restored": False,
        }
        baseline_mono = None
        known_pods = {}
        initial_worker = None
        previous_receipt = {}
        started = time.monotonic()
        outcome = None
        try:
            while time.monotonic() - started < 1000:
                sample_started_mono = time.monotonic()
                events = [e async for e in handle.fetch_history_events()]
                data.update(
                    await decode_history(events, self.client.data_converter, inp.session_id)
                )
                pod_rows, workers = await asyncio.gather(
                    asyncio.to_thread(self.ops.sandbox_rows, inp.session_id),
                    asyncio.to_thread(self.ops.workers),
                )
                for pod in pod_rows:
                    known_pods[pod["name"]] = pod
                sample = {"observed_at": time.time(), "pods": pod_rows, "workers": workers}
                if case == "aps_down":
                    sample["aps"] = await asyncio.to_thread(self.ops.sample)
                try:
                    observation = await asyncio.to_thread(self.observer.read, inp.session_id)
                    sample["observer"] = observation
                except ApplicationError as exc:
                    if case != "aps_down" or exc.type != "APSUnavailable" or baseline_mono is None:
                        raise
                    sample["observer_error"] = "APSUnavailable"
                    observation = None
                current_gates = (
                    await gates(handle, self.client.data_converter, inp.session_id)
                    if not data["terminal"]
                    else []
                )
                if baseline_mono is None:
                    boundary = [g for g in current_gates if g["stage"] == "boundary1"]
                    if boundary and held(observation):
                        if (
                            len(boundary) != 1
                            or len(pod_rows) != 1
                            or len(workers) != 1
                            or not workers[0]["ready"]
                            or not workers[0].get("owner_uid")
                            or workers[0]["image"] != self.cfg["worker_image"]
                            or boundary[0]["worker_uid"] != workers[0]["uid"]
                        ):
                            raise ValueError(
                                "initial lifecycle gate is not on the exact Ready isolated worker"
                            )
                        initial_worker = workers[0]
                        data.update(
                            initial=observation,
                            initial_pods=pod_rows,
                            baseline_gate=boundary[0],
                            initial_worker=workers[0],
                        )
                        if case == "aps_down":
                            data["aps_initial"] = sample["aps"]
                            if not aps_ready(sample["aps"]):
                                raise ValueError(
                                    "APS must have two Ready replicas before the lifecycle fault"
                                )
                        # This local instant precedes the held-state reads and
                        # baseline_ready, hence every authorized fault/cleanup.
                        # Remote wall clocks must never shorten this upper bound.
                        baseline_mono = sample_started_mono
                        self.notify(case, "baseline_ready", pod=pod_rows[0], worker=initial_worker)
                        if case == "terminate":
                            await handle.terminate(reason="T24 exact hard-termination case")
                        elif case == "cancel":
                            await handle.cancel()
                    elif data["terminal"] or time.monotonic() - started > 240:
                        raise ValueError(
                            "initial genuine grant/vault/checkpoint gate was not observed"
                        )
                    await asyncio.sleep(0.25)
                    continue
                data["samples"].append(sample)
                current = safe_receipt(
                    await asyncio.to_thread(self.control.get, "receipt-" + case),
                    case,
                    inp.session_id,
                )
                if any(current.get(k) != v for k, v in previous_receipt.items()):
                    raise ValueError("operator removed or changed a lifecycle receipt")
                previous_receipt = current
                data["receipt"] = current
                if case == "worker_kill":
                    cleanup_gates = [
                        g for g in current_gates if g["stage"] in {"delete_sandbox", "delete_grant"}
                    ]
                    if len(cleanup_gates) == 2 and any(
                        r["activity"] == "stop_session" and r.get("result") == "OK"
                        for r in data["history"]
                    ):
                        if not data["cleanup_gates"]:
                            data["cleanup_gates"] = cleanup_gates
                        self.notify(
                            case, "cleanup_waiting_kill_exact_worker", worker=initial_worker
                        )
                if data["terminal"]:
                    self.notify(case, "cleanup_recorded_restore_fault_now")
                anchors = [
                    r["scheduled_at"]
                    for r in data["history"]
                    if r["activity"] in {"stop_session", "delete_sandbox", "delete_grant"}
                ]
                anchor = (
                    data["terminal"].get("at")
                    if case == "terminate"
                    else min(anchors)
                    if anchors
                    else None
                )
                if case == "activity_crash" and current:
                    anchor = current["started_at"]
                if anchor is not None:
                    data["anchor"] = anchor
                data["elapsed_monotonic"] = time.monotonic() - baseline_mono
                if data["elapsed_monotonic"] > 600:
                    break
                if case in {"aps_down", "finalizer", "worker_kill", "activity_crash"}:
                    data["restored"] = bool(current.get("restored_at"))
                    if case == "aps_down":
                        data["restored"] &= aps_ready(sample["aps"])
                    elif case == "finalizer":
                        data["restored"] &= not any(FINALIZER in p["finalizers"] for p in pod_rows)
                    else:
                        data["worker_absence"] = await asyncio.to_thread(
                            self.ops.absent, "agent-platform", initial_worker
                        )
                        data["restored"] &= (
                            data["worker_absence"]["http_status"] == 404
                            and len(workers) == 1
                            and workers[0]["ready"]
                            and workers[0]["uid"] != initial_worker["uid"]
                            and workers[0]["image"] == self.cfg["worker_image"]
                            and workers[0].get("owner_uid") == initial_worker["owner_uid"]
                        )
                else:
                    data["restored"] = True
                if data["terminal"] and clean(observation) and not pod_rows and data["restored"]:
                    data["reconciler"] = await self.reconciler(schedule, inp.session_id)
                    # Store convergence can precede completion of the activity
                    # reporting it. Observe that real receipt before finishing;
                    # never reissue cleanup or trigger the Schedule to manufacture one.
                    if case in {"terminate", "aps_down"} and (
                        not result(data["reconciler"], "close_session", "OK")
                        or (
                            case == "terminate"
                            and not result(data["reconciler"], "delete_orphan", "DELETED")
                        )
                    ):
                        await asyncio.sleep(0.25)
                        continue
                    # Create receipts cover even a generation too brief for polling.
                    for row in data["history"]:
                        if row["activity"] == "create_sandbox" and isinstance(
                            row.get("result"), str
                        ):
                            known_pods.setdefault(
                                row["result"], {"name": row["result"], "uid": None}
                            )
                    data["pod_absence"] = await asyncio.gather(
                        *(
                            asyncio.to_thread(self.ops.absent, inp.tenant, pod)
                            for pod in known_pods.values()
                        )
                    )
                    data.update(
                        final=observation,
                        final_pods=pod_rows,
                        # Include all final receipt/404 reads in the upper bound.
                        elapsed_monotonic=time.monotonic() - baseline_mono,
                    )
                    break
                await asyncio.sleep(0.5)
            outcome = evaluate_case(case, data)
        except Exception as exc:
            outcome = {"result": "error", "error": type(exc).__name__, "evidence": data}
        finally:
            try:
                await settled(self.cleanup._close(inp, handle, task))
            except Exception as exc:
                if outcome is None:
                    raise
                outcome = {**outcome, "result": "error", "cleanup_error": type(exc).__name__}
        return outcome

    async def t24(self):
        cases = []
        try:
            fixture = await self.validate()
            for case in CASES:
                outcome = await self.run_case(case)
                cases.append({"case": case, **outcome})
                if outcome["result"] != "pass":
                    break
                if await self.validate() != fixture:
                    raise ValueError("lifecycle fixture changed during qualification")
            checks = {
                "all_four_required_faults": {c["case"] for c in cases if c["result"] == "pass"}
                >= set(FAULTS),
                "all_supplemental_cases": len(cases) == len(CASES)
                and all(c["result"] == "pass" for c in cases),
            }
            return {
                "id": "T24",
                "result": "pass" if all(checks.values()) else "fail",
                "checks": checks,
                "cases": cases,
                "scope": "Four exact cleanup faults plus success/cancel/activity-loss/retry-exhaustion; production eight-hour business-timer duration is not qualified by this batch.",
            }
        except Exception as exc:
            return {"id": "T24", "result": "error", "error": type(exc).__name__, "cases": cases}
