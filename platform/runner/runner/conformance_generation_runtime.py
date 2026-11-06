"""Read-only T21 observations; root controls faults, trusted isolated worker acts."""

from __future__ import annotations

import asyncio
import functools
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

from kubernetes.client.exceptions import ApiException
from temporalio.api.enums.v1 import PendingActivityState
from temporalio.client import ScheduleOverlapPolicy
from temporalio.common import WorkflowIDReusePolicy

from runner.activities import pod_name
from runner.conformance_admission import KubernetesAdmission, ready, settled
from runner.conformance_generation import evaluate_case
from runner.conformance_generation_control import CASES, FINALIZER
from runner.conformance_generation_worker import marker
from runner.conformance_lifecycle_runtime import decode_history
from runner.conformance_revocation import LiveEvidence
from runner.types import SessionInput
from runner.workflow import EXECUTION_TIMEOUT, SessionWorkflow
from runner.worker_operations import bounded_history


RPC_TIMEOUT = timedelta(seconds=5)
OBSERVATION_TIMEOUT = 30
CASE_TIMEOUT = 960
CLEANUP_TIMEOUT = 240
HISTORY_EVENTS = 10_000
HISTORY_BYTES = 8 << 20
HISTORY_PAGES = 100


def bounded_observation(function):
    @functools.wraps(function)
    async def bounded(*args, **kwargs):
        async with asyncio.timeout(OBSERVATION_TIMEOUT):
            return await function(*args, **kwargs)

    return bounded


async def read_io(function, *args, **kwargs):
    async with asyncio.timeout(OBSERVATION_TIMEOUT):
        return await asyncio.to_thread(function, *args, **kwargs)


async def events_for(handle):
    async with asyncio.timeout(OBSERVATION_TIMEOUT):
        history, _ = await bounded_history(
            handle,
            handle.id,
            max_events=HISTORY_EVENTS,
            max_bytes=HISTORY_BYTES,
            max_pages=HISTORY_PAGES,
        )
        return history.events


def source_stamp(pod):
    statuses = pod.status.container_statuses or []
    if (
        pod.metadata.deletion_timestamp
        or pod.status.phase != "Running"
        or not statuses
        or not all(s.ready and s.image_id for s in statuses)
    ):
        raise ValueError("T21 log source is not Ready and pinned")
    return {
        "uid": pod.metadata.uid,
        "service_account": pod.spec.service_account_name,
        "images": sorted((c.name, c.image) for c in pod.spec.containers),
        "containers": sorted(
            (s.name, s.image_id, s.container_id, s.restart_count, s.ready) for s in statuses
        ),
        "ephemeral_containers": len(pod.spec.ephemeral_containers or []),
    }


def raw_log(core, namespace, name, container=None):
    response = core.read_namespaced_pod_log(
        name,
        namespace,
        **({"container": container} if container else {}),
        limit_bytes=4194304,
        _preload_content=False,
        _request_timeout=5,
    )
    try:
        raw = response.read(4194305)
        if len(raw) > 4194304:
            raise ValueError("T21 log bound exceeded")
        return raw.decode("utf-8", errors="strict")
    finally:
        response.close()
        response.release_conn()


def removal_timing(receipt, held_started_ns, observed_ns):
    if (
        type(held_started_ns) is not int
        or type(observed_ns) is not int
        or not 0 < held_started_ns <= observed_ns
    ):
        raise ValueError("T21 requires ordered harness monotonic held/cleanup observations")
    return {
        "root_removal_clock": {
            k: receipt[k]
            for k in ("clock_boot_id", "removal_started_ns", "removal_acknowledged_ns")
        },
        "harness_monotonic_bound": True,
        "reconciler_after_removal_seconds": (observed_ns - held_started_ns) / 1e9,
    }


class GenerationOps(KubernetesAdmission):
    def __init__(self, core, custom, control):
        super().__init__(core, custom, "tenant-test")
        self.control = control

    def rows(self, session):
        return [
            {
                "name": p["metadata"]["name"],
                "uid": p["metadata"]["uid"],
                "generation": int(p["metadata"]["labels"]["agentplatform.io/generation"]),
                "phase": p.get("status", {}).get("phase"),
                "deleting": bool(p["metadata"].get("deletionTimestamp")),
                "finalizers": p["metadata"].get("finalizers", []),
            }
            for p in self.pods(session)
        ]

    def workers(self):
        pods = self.core.list_namespaced_pod(
            "agent-platform",
            label_selector="agentplatform.io/generation-run=" + self.control.config["run_id"],
            _request_timeout=5,
        ).items
        return [
            {
                "name": p.metadata.name,
                "uid": p.metadata.uid,
                "image": p.spec.containers[0].image,
                "image_id": "|".join(
                    sorted(s.image_id or "" for s in p.status.container_statuses or [])
                ),
                "service_account": p.spec.service_account_name,
                "ready": p.status.phase == "Running"
                and not p.metadata.deletion_timestamp
                and bool(p.status.container_statuses)
                and all(s.ready for s in p.status.container_statuses),
                "restarts": sum(s.restart_count for s in p.status.container_statuses or []),
                "source_stamp": source_stamp(p),
            }
            for p in pods
        ]

    def receipts(self, worker, case, session):
        if self.workers() != [worker]:
            raise ValueError("T21 isolated worker changed")
        rows = []
        text = raw_log(self.core, "agent-platform", worker["name"], "runner")
        if self.workers() != [worker]:
            raise ValueError("T21 isolated worker changed during log read")
        for line in text.splitlines():
            try:
                row = json.loads(line[line.index("{") :])
            except (ValueError, TypeError):
                continue
            if (
                isinstance(row, dict)
                and row.get("event") == "t21_worker_receipt"
                and row.get("run_id") == self.control.config["run_id"]
                and row.get("case") == case
                and row.get("session_id") == session
                and row.get("worker_uid") == worker["uid"]
            ):
                rows.append(
                    {
                        k: row.get(k)
                        for k in (
                            "operation",
                            "session_id",
                            "worker_uid",
                            "pod_uid",
                            "generation",
                            "acknowledged",
                            "observed",
                            "error_type",
                        )
                    }
                )
        return rows

    def absent(self, name):
        try:
            self.core.read_namespaced_pod(name, self.tenant, _request_timeout=5)
        except ApiException as exc:
            if exc.status == 404:
                return True
            raise
        return False


class Traffic(LiveEvidence):
    def discover(self):
        super().discover()
        self.source_stamps = {}
        for source in self.sources:
            ns, name, _, kind = source
            stamp = source_stamp(self.core.read_namespaced_pod(name, ns, _request_timeout=5))
            if kind == "aps" and stamp["uid"] != self.replicas[name]:
                raise ValueError("T21 APS replaced during discovery")
            self.source_stamps[source] = stamp

    @bounded_observation
    async def sample(self):
        async def read(source):
            ns, name, container, kind = source
            before = await read_io(self.core.read_namespaced_pod, name, ns, _request_timeout=5)
            if source_stamp(before) != self.source_stamps[source]:
                raise ValueError("T21 log source changed before read")
            text = await read_io(raw_log, self.core, ns, name, container)
            after = await read_io(self.core.read_namespaced_pod, name, ns, _request_timeout=5)
            if source_stamp(after) != self.source_stamps[source]:
                raise ValueError("T21 log source changed during read")
            observed = time.monotonic_ns()
            if kind == "aps":
                self.ingest_aps(name, text, observed)
            else:
                self.ingest_gateway(text, observed)

        await asyncio.gather(*(read(source) for source in self.sources))


@bounded_observation
async def pending_gates(handle, converter, session):
    result = []
    description = await handle.describe(rpc_timeout=RPC_TIMEOUT)
    if len(description.raw_description.pending_activities) > 16:
        raise ValueError("T21 pending-activity bound exceeded")
    for pending in description.raw_description.pending_activities:
        if not pending.HasField("heartbeat_details"):
            continue
        if (
            len(pending.heartbeat_details.payloads) > 4
            or pending.heartbeat_details.ByteSize() > 32768
        ):
            raise ValueError("T21 heartbeat bound exceeded")
        for value in await converter.decode(list(pending.heartbeat_details.payloads)):
            if (
                isinstance(value, dict)
                and value.get("schema") == "t21-gate-v1"
                and value.get("session_id") == session
            ):
                result.append(
                    {
                        k: value.get(k)
                        for k in ("schema", "case", "run_id", "session_id", "pod_uid", "worker_uid")
                    }
                )
    return result


class GenerationHarness:
    def __init__(self, client, ops, control):
        self.client, self.ops, self.control = client, ops, control
        self.cfg = control.config
        self.notified = set()
        self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def notify(self, case, phase, **metadata):
        if (case, phase) in self.notified:
            return
        print(
            json.dumps(
                {
                    "test": "T21",
                    "run_id": self.cfg["run_id"],
                    "case": case,
                    "session_id": self.cfg["sessions"][case],
                    "phase": phase,
                    "emitted_monotonic_ns": time.monotonic_ns(),
                    "clock_boot_id": self.boot_id,
                    **metadata,
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        self.notified.add((case, phase))

    def inp(self, case):
        sid = self.cfg["sessions"][case]
        if case == "fence_only":
            return SessionInput(
                "tenant-test",
                "conformance-b",
                self.cfg["probe_release"],
                sid,
                None,
                {"tool": "probe_revocation", "args": {"marker": marker(sid), "duration": "180"}},
                max_boundaries=8,
            )
        return SessionInput(
            "tenant-test",
            "conformance-checkpoint",
            self.cfg["recovery_release"],
            sid,
            None,
            {"checkpoint_case": "a", "run_id": self.cfg["run_id"]},
            max_boundaries=3,
        )

    @bounded_observation
    async def schedule(self):
        description = await self.client.get_schedule_handle(
            "cleanup-reconciler-tenant-test"
        ).describe(rpc_timeout=RPC_TIMEOUT)
        schedule = description.schedule
        if (
            schedule.state.paused
            or schedule.action.workflow != "CleanupReconcilerWorkflow"
            or schedule.action.task_queue != "cleanup-tenant-test"
            or schedule.policy.overlap != ScheduleOverlapPolicy.SKIP
            or len(schedule.spec.intervals) != 1
            or schedule.spec.intervals[0].every.total_seconds() != 300
        ):
            raise ValueError("T21 requires unchanged ordinary five-minute reconciler Schedule")
        return description

    @bounded_observation
    async def reconciler(self, baseline, inp):
        current = await self.schedule()
        if (
            current.schedule != baseline.schedule
            or current.info.last_updated_at != baseline.info.last_updated_at
        ):
            raise ValueError("T21 reconciler Schedule changed")
        expected = set(baseline.info.next_action_times)
        rows = []
        for action in current.info.recent_actions:
            if action.scheduled_at not in expected:
                continue
            handle = self.client.get_workflow_handle(
                action.action.workflow_id, run_id=action.action.first_execution_run_id
            )
            events = await events_for(handle)
            decoded = await decode_history(
                events, self.client.data_converter, inp.session_id, expected_agent=inp.agent
            )
            description = await handle.describe(rpc_timeout=RPC_TIMEOUT)
            pending_ids = {
                p.activity_id
                for p in description.raw_description.pending_activities
                if p.activity_type.name == "delete_orphan"
                and p.state == PendingActivityState.PENDING_ACTIVITY_STATE_STARTED
            }
            for row in decoded["history"]:
                row["pending_started"] = row["activity_id"] in pending_ids
            rows.extend(
                {
                    **row,
                    "scheduled_action": True,
                    "workflow_id": action.action.workflow_id,
                    "run_id": action.action.first_execution_run_id,
                }
                for row in decoded["history"]
            )
        return rows

    @bounded_observation
    async def history(self, handle, inp):
        events = await events_for(handle)
        decoded = await decode_history(
            events, self.client.data_converter, inp.session_id, expected_agent=inp.agent
        )
        for row in decoded["history"]:
            if row.get("completed_at") is not None:
                row["duration"] = row["completed_at"] - row["scheduled_at"]
        return decoded

    @bounded_observation
    async def validate(self):
        await read_io(self.control.read)
        for agent, key in (
            ("conformance-b", "probe_release"),
            ("conformance-checkpoint", "recovery_release"),
        ):
            release = await read_io(self.ops.object, "agentreleases", self.cfg[key])
            status = release.get("status", {})
            permits_no_grant = status.get("identityMode") == "agent" or (
                status.get("identityMode") == "on-behalf-of"
                and status.get("delegationRequired") is False
            )
            if (
                not ready(release)
                or release.get("spec", {}).get("agent") != agent
                or not permits_no_grant
            ):
                raise ValueError("T21 requires exact Ready fixtures permitting no-grant sessions")
        worker = await read_io(self.ops.workers)
        if (
            len(worker) != 1
            or not worker[0]["ready"]
            or worker[0]["image"] != self.cfg["worker_image"]
            or not worker[0]["image_id"]
            or worker[0]["service_account"] != "session-runner"
        ):
            raise ValueError("T21 isolated trusted worker is not exact Ready pinned runner")
        return worker[0]

    async def case(self, case):
        inp = self.inp(case)
        data = {
            "session_id": inp.session_id,
            "samples": [],
            "history": [],
            "worker_receipts": [],
            "cleanup": {"session_closed": False, "pods_absent": False, "finalizer_absent": False},
        }
        handle = traffic = None
        began = time.monotonic()
        held_start = None
        try:
            async with asyncio.timeout(CASE_TIMEOUT):
                worker = await self.validate()
                data["worker_before"] = worker
                if await read_io(self.ops.pods, inp.session_id):
                    raise ValueError("T21 session already has Pods")
                schedule = await self.schedule() if case == "blocked_deletion" else None

                async def start():
                    nonlocal handle
                    try:
                        handle = await self.client.start_workflow(
                            SessionWorkflow.run,
                            inp,
                            id=inp.workflow_id,
                            task_queue=self.cfg["queue"],
                            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                            execution_timeout=EXECUTION_TIMEOUT,
                            rpc_timeout=timedelta(seconds=5),
                        )
                    except Exception:
                        handle = self.client.get_workflow_handle(inp.workflow_id)
                        raise

                await settled(start())
                while time.monotonic() - began < CASE_TIMEOUT:
                    await read_io(self.control.read)
                    sample_started_ns = time.monotonic_ns()
                    pods = await read_io(self.ops.rows, inp.session_id)
                    decoded = await self.history(handle, inp)
                    data.update(decoded)
                    data.update(decoded.get("progress") or {})
                    if "initial_pod" not in data:
                        running = [
                            p
                            for p in pods
                            if p["generation"] == 1
                            and p["phase"] == "Running"
                            and not p["deleting"]
                        ]
                        if len(running) == 1:
                            data["initial_pod"] = running[0]
                        elif bool((data.get("terminal") or {}).get("kind")):
                            raise ValueError("T21 session closed before Running generation 1")
                        else:
                            await asyncio.sleep(0.25)
                            continue
                    if any(
                        p["generation"] == 1 and p["uid"] != data["initial_pod"]["uid"]
                        for p in pods
                    ):
                        raise ValueError("T21 generation-1 UID changed")
                    receipts = await read_io(self.ops.receipts, worker, case, inp.session_id)
                    data["worker_receipts"] = receipts
                    if any(r["operation"] == "fence_error" for r in receipts):
                        raise ValueError("trusted fence operation failed")
                    data["samples"].append({"elapsed": time.monotonic() - began, "pods": pods})
                    if len(data["samples"]) > 4096:
                        raise ValueError("T21 sample bound exceeded")
                    if case == "fence_only":
                        if traffic is None:
                            traffic = Traffic(
                                self.ops.core,
                                inp.session_id,
                                data["initial_pod"]["name"],
                                marker(inp.session_id),
                            )
                            await read_io(traffic.discover)
                            data["aps_replicas"] = sorted(traffic.replicas)
                        await traffic.sample()
                        items = traffic.items()
                        allowed = [
                            r for r in items if r["status"] == 200 and r["reason"] == "allow"
                        ]
                        if traffic.allowed() and "allowed_requests" not in data:
                            data["allowed_requests"] = allowed
                            self.notify(
                                case, "ready_fence_only", pod=data["initial_pod"], worker=worker
                            )
                        denied = [
                            r
                            for r in items
                            if r["status"] == 403 and r["reason"] == "deny.generation_stale"
                        ]
                        data["stale_requests"] = denied
                        if {r["source_pod"] for r in denied} == set(traffic.replicas) and any(
                            r["operation"] == "fence_generation_2" for r in receipts
                        ):
                            data["samples"][-1]["phase"] = "stale_denial"
                            break
                        if (
                            bool((data.get("terminal") or {}).get("kind"))
                            or time.monotonic() - began > 240
                        ):
                            raise TimeoutError("T21 genuine fixed-token fencing evidence absent")
                    else:
                        gates = (
                            await pending_gates(handle, self.client.data_converter, inp.session_id)
                            if not bool((data.get("terminal") or {}).get("kind"))
                            else []
                        )
                        matching = [
                            g
                            for g in gates
                            if g["case"] == case
                            and g["run_id"] == self.cfg["run_id"]
                            and g["pod_uid"] == data["initial_pod"]["uid"]
                            and g["worker_uid"] == worker["uid"]
                        ]
                        if matching:
                            self.notify(
                                case,
                                "ready_delete_for_recovery",
                                pod=data["initial_pod"],
                                worker=worker,
                                finalizer=FINALIZER if case == "blocked_deletion" else None,
                            )
                        if case == "recovery_control" and bool(
                            (data.get("terminal") or {}).get("kind")
                        ):
                            break
                        if case == "blocked_deletion" and bool(
                            (data.get("terminal") or {}).get("kind")
                        ):
                            if data.get("outcome") != "NOT_CONFIRMED":
                                raise ValueError("blocked recovery did not end NOT_CONFIRMED")
                            if held_start is None:
                                held_start = time.monotonic()
                            rows = await self.reconciler(schedule, inp)
                            data["reconciler"] = rows
                            pending = [
                                r
                                for r in rows
                                if r["activity"] == "delete_orphan"
                                and r.get("pod_name") == data["initial_pod"]["name"]
                                and r.get("pending_started") is True
                                and r.get("completed_at") is None
                            ]
                            receipt = await read_io(
                                self.control.finalizer_receipt, data["initial_pod"]["uid"]
                            )
                            data["finalizer_receipt"] = receipt
                            if pending and any(
                                p["uid"] == data["initial_pod"]["uid"]
                                and FINALIZER in p["finalizers"]
                                and p["deleting"]
                                for p in pods
                            ):
                                data["scheduled_gate_before_removal"] = True
                                data.setdefault(
                                    "held_before_removal_monotonic_ns", sample_started_ns
                                )
                                data["held_wait_seconds"] = time.monotonic() - held_start
                                self.notify(
                                    case,
                                    "scheduled_delete_pending_remove_finalizer_now",
                                    pod=data["initial_pod"],
                                    worker=worker,
                                    finalizer=FINALIZER,
                                )
                            if receipt and receipt["finalizer_removed"]:
                                # Root and Pod clocks may differ. Preserve root PATCH
                                # bounds only as provenance; elapsed uses this harness's
                                # own earlier observation of the still-held exact Pod.
                                data["root_removal_clock"] = {
                                    k: receipt[k]
                                    for k in (
                                        "clock_boot_id",
                                        "removal_started_ns",
                                        "removal_acknowledged_ns",
                                    )
                                }
                                anchor = data.get("held_before_removal_monotonic_ns")
                                if type(anchor) is not int:
                                    raise ValueError("finalizer removed without recorded held gate")
                                data["harness_monotonic_bound"] = True
                                absent = await read_io(self.ops.absent, data["initial_pod"]["name"])
                                completed = any(
                                    r["activity"] == "delete_orphan"
                                    and r.get("pod_name") == data["initial_pod"]["name"]
                                    and r.get("result") == "DELETED"
                                    for r in rows
                                )
                                timing = removal_timing(receipt, anchor, time.monotonic_ns())
                                elapsed = timing["reconciler_after_removal_seconds"]
                                if absent and completed:
                                    data.update(timing)
                                    break
                                if elapsed > 300:
                                    raise TimeoutError(
                                        "T21 scheduled deletion confirmation exceeded 300 seconds"
                                    )
                    await asyncio.sleep(0.25)
                else:
                    raise TimeoutError("T21 case deadline")
        except Exception as exc:
            data["error_type"] = type(exc).__name__
        finally:

            async def close():
                if case == "blocked_deletion" and "initial_pod" in data:
                    self.notify(
                        case,
                        "restore_exact_finalizer_now",
                        pod=data["initial_pod"],
                        finalizer=FINALIZER,
                    )
                if handle is not None:
                    try:
                        if not (data.get("terminal") or {}).get("kind"):
                            await handle.cancel(rpc_timeout=RPC_TIMEOUT)
                        until_closed = time.monotonic() + 180
                        while time.monotonic() < until_closed:
                            decoded = await self.history(handle, inp)
                            data.update(decoded)
                            data.update(decoded.get("progress") or {})
                            if (data.get("terminal") or {}).get("kind"):
                                data["cleanup"]["session_closed"] = True
                                break
                            await asyncio.sleep(0.25)
                        if not data["cleanup"]["session_closed"]:
                            raise TimeoutError("T21 session closure was not confirmed")
                    except Exception as exc:
                        data["cleanup"]["session_error_type"] = type(exc).__name__
                until = time.monotonic() + 60
                while time.monotonic() < until:
                    rows = await read_io(self.ops.rows, inp.session_id)
                    data["cleanup"]["finalizer_absent"] = not any(
                        FINALIZER in p["finalizers"] for p in rows
                    )
                    if not rows and await read_io(self.ops.absent, pod_name(inp.session_id, 1)):
                        data["cleanup"]["pods_absent"] = True
                        break
                    await asyncio.sleep(0.25)
                if handle is not None:
                    try:
                        decoded = await self.history(handle, inp)
                        data.update(decoded)
                        data.update(decoded.get("progress") or {})
                    except Exception as exc:
                        data["cleanup"]["history_error_type"] = type(exc).__name__
                workers = await read_io(self.ops.workers)
                if workers != [data.get("worker_before")]:
                    raise ValueError("T21 isolated worker changed at cleanup")
                data["worker_after"] = workers[0]

            try:
                await settled(asyncio.wait_for(close(), timeout=CLEANUP_TIMEOUT))
            except Exception as exc:
                data["cleanup"]["error_type"] = type(exc).__name__
        return evaluate_case(case, data)

    async def t21(self):
        results = []
        for case in CASES:
            result = await self.case(case)
            results.append(result)
            if result["result"] != "pass":
                break
        return {
            "id": "T21",
            "name": "separate generation fence and confirmed-delete recovery",
            "result": "pass"
            if len(results) == len(CASES) and all(r["result"] == "pass" for r in results)
            else "fail",
            "cases": results,
        }
