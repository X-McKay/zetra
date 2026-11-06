"""Read-only T27 harness: ordinary signed fixture/T4 workflows plus operator capture."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import asdict

from temporalio.common import WorkflowIDReusePolicy

from runner.activities import pod_name
from runner.conformance import (
    Harness,
    SessionRun,
    boundaries_from_events,
    failures_from_events,
    utcnow,
)
from runner.conformance_admission import AdmissionSessions, KubernetesAdmission, ready, settled
from runner.conformance_transport import evaluate, finite, read_probe
from runner.conformance_transport_control import (
    GATE_FIELDS,
    Control,
    capture_started,
    validate_gate,
)
from runner.types import SessionInput
from runner.workflow import EXECUTION_TIMEOUT, SessionWorkflow


async def transport_gates(handle, converter, session):
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
            or values[0].get("schema") != "t27-gate-v1"
        ):
            continue
        value = values[0]
        if (
            set(value) != GATE_FIELDS | {"schema", "phase", "observed_at"}
            or value["phase"] not in {"before_exec", "after_exec"}
            or not finite(value.get("observed_at"))
            or value["observed_at"] <= 0
            or value["session_id"] != session
        ):
            raise ValueError("transport heartbeat binding differs from exact session")
        validate_gate({k: value[k] for k in GATE_FIELDS})
        rows.append(value)
    return rows


def valid_workers(rows, expected):
    test = [r for r in rows if r.get("test_worker")]
    production = [r for r in rows if not r.get("test_worker")]
    return (
        bool(production)
        and len(test) == 1
        and test[0].get("restarts") == 0
        and all(
            r.get("ready") is True
            and r.get("image") == expected
            and str(r.get("image_id", "")).split("@")[-1] == expected.split("@")[-1]
            and bool(r.get("uid"))
            for r in rows
        )
    )


class KubernetesTransport(KubernetesAdmission):
    def workers(self, run_id):
        pods = self.core.list_namespaced_pod(
            "agent-platform",
            label_selector="app.kubernetes.io/name=session-runner,agentplatform.io/tenant=tenant-test",
            _request_timeout=5,
        ).items
        rows = []
        for pod in pods:
            statuses = [s for s in pod.status.container_statuses or [] if s.name == "runner"]
            specs = [s for s in pod.spec.containers if s.name == "runner"]
            status = statuses[0] if len(statuses) == 1 else None
            rows.append(
                {
                    "name": pod.metadata.name,
                    "uid": pod.metadata.uid,
                    "image": specs[0].image if len(specs) == 1 else None,
                    "image_id": status.image_id if status else None,
                    "ready": bool(
                        status
                        and status.ready
                        and pod.status.phase == "Running"
                        and not pod.metadata.deletion_timestamp
                    ),
                    "restarts": status.restart_count if status else None,
                    "test_worker": (pod.metadata.labels or {}).get("agentplatform.io/transport-run")
                    == run_id,
                }
            )
        return sorted(rows, key=lambda row: row["name"])


class BoundT4(Harness):
    def __init__(self, parent):
        base = parent.base
        super().__init__(
            base.client,
            "tenant-test",
            "conformance",
            parent.cfg["t4_release"],
            base.aps_logs,
            artifact_dir=base.artifact_dir,
            issuer=base.issuer,
            session_timeout=base.session_timeout,
        )
        self.parent = parent

    async def run_session(self, case, **_):
        return await self.parent.session("t4", case.task())


class TransportHarness:
    def __init__(self, ops, client, base, release):
        self.ops, self.client, self.base = ops, client, base
        self.control = Control(ops.core)
        self.cfg = self.control.config
        if (
            release != self.cfg["transport_release"]
            or base.release != self.cfg["t4_release"]
            or base.tenant != "tenant-test"
            or base.agent != "conformance"
        ):
            raise ValueError("T27 releases/identity differ from operator-bound configuration")
        self.cleanup = AdmissionSessions(client, ops, timeout=180)
        self.data = {"evidence_errors": [], "sessions": [], "cleanup_confirmed": False}
        self.notified = set()

    def notify(self, phase, **value):
        if phase not in self.notified:
            print(
                json.dumps({"test": "T27", "run_id": self.cfg["run_id"], "phase": phase, **value}),
                file=sys.stderr,
                flush=True,
            )
            self.notified.add(phase)

    async def validate(self):
        rows = []
        for name, agent in (
            ("transport_release", "conformance-transport"),
            ("t4_release", "conformance"),
        ):
            release = await asyncio.to_thread(self.ops.release, self.cfg[name])
            if release.get("spec", {}).get("agent") != agent or not ready(release):
                raise ValueError("T27 requires the exact signed Ready approved releases")
            rows.append(
                {"name": self.cfg[name], "uid": release["metadata"]["uid"], "spec": release["spec"]}
            )
        return rows

    async def observe_gate(self, handle, session):
        rows = await transport_gates(handle, self.client.data_converter, session)
        if not rows:
            return
        if len(rows) != 1:
            raise ValueError("T27 has ambiguous live activity gates")
        row = rows[0]
        gate = {k: row[k] for k in GATE_FIELDS}
        if gate["run_id"] != self.cfg["run_id"] or gate["pod"] != pod_name(session, 1):
            raise ValueError("T27 gate changed run or deterministic Pod")
        if "gate" in self.data and gate != self.data["gate"]:
            raise ValueError("T27 exact capture gate changed")
        workers = await asyncio.to_thread(self.ops.workers, self.cfg["run_id"])
        if (
            workers != self.data["workers_before"]
            or not valid_workers(workers, self.cfg["worker_image"])
            or gate["worker_uid"] != next(w["uid"] for w in workers if w["test_worker"])
        ):
            raise ValueError("T27 gate worker identity or image changed")
        pods = await asyncio.to_thread(self.ops.pods, session)
        if (
            len(pods) != 1
            or pods[0]["metadata"]["uid"] != gate["pod_uid"]
            or pods[0]["status"].get("podIP") != gate["pod_ip"]
            or pods[0]["spec"].get("nodeName") != gate["node"]
        ):
            raise ValueError("T27 gate sandbox identity changed")
        self.data["gate"] = gate
        gate_times = self.data.setdefault("gate_times", {})
        if row["phase"] in gate_times and gate_times[row["phase"]] != row["observed_at"]:
            raise ValueError("transport gate phase timestamp changed")
        gate_times[row["phase"]] = row["observed_at"]
        self.notify(
            "capture_start_required" if row["phase"] == "before_exec" else "capture_stop_required",
            gate=gate,
        )
        start = await asyncio.to_thread(self.control.get, "capture-start")
        if start is not None:
            capture_started(start, gate)
            self.data["capture_start"] = start
        complete = await asyncio.to_thread(self.control.get, "capture-complete")
        if complete is not None:
            if complete.get("capture_start") != start:
                raise ValueError("T27 capture completion start differs")
            self.data["capture_complete"] = complete

    async def session(self, case, task):
        cfg = self.cfg
        inp = SessionInput(
            "tenant-test",
            "conformance" if case == "t4" else "conformance-transport",
            cfg["t4_release" if case == "t4" else "transport_release"],
            cfg["sessions"][case],
            None,
            task,
        )
        inp.validate()
        handle = await self.client.start_workflow(
            SessionWorkflow.run,
            inp,
            id=inp.workflow_id,
            task_queue=cfg["queue"],
            execution_timeout=EXECUTION_TIMEOUT,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        result_task = asyncio.create_task(handle.result())
        row = {"case": case, "session_id": inp.session_id, "cleanup_confirmed": False}
        self.data["sessions"].append(row)
        run = SessionRun(inp.session_id, pod_name(inp.session_id, 1), "SUBMITTED", started=utcnow())
        try:
            until = time.monotonic() + self.base.session_timeout
            while not result_task.done():
                if case == "t4":
                    await self.observe_gate(handle, inp.session_id)
                if time.monotonic() >= until:
                    raise TimeoutError("T27 session timeout")
                await asyncio.sleep(0.2)
            result = await result_task
            row.update(result=asdict(result), finished=utcnow())
            run.outcome, run.generation, run.snapshot = (
                result.outcome,
                result.generation,
                result.snapshot,
            )
            run.pod, run.finished = pod_name(inp.session_id, result.generation), utcnow()
            events = [event async for event in handle.fetch_history_events()]
            boundaries = await boundaries_from_events(events, self.client.data_converter)
            run.boundaries = [asdict(b) for b in boundaries]
            run.failures = await failures_from_events(events, self.client.data_converter)
            for boundary in reversed(boundaries):
                if boundary.kind == "model" and boundary.summary:
                    run.final_summary = boundary.summary
                    break
            row["run"] = asdict(run)
            if result.cleanup != {"stop": "OK", "delete": "DELETED", "grant": "OK"}:
                raise ValueError("T27 session cleanup did not confirm all outcomes")
            if case == "t4":
                # Final heartbeat can vanish as the activity returns. Receipts
                # remain immutable and are read once more after workflow closure.
                self.data["capture_start"] = await asyncio.to_thread(
                    self.control.get, "capture-start"
                )
                self.data["capture_complete"] = await asyncio.to_thread(
                    self.control.get, "capture-complete"
                )
            return run
        finally:
            await settled(self.cleanup._close(inp, handle, result_task))
            row["cleanup_confirmed"] = True

    async def t27(self):
        try:
            fixture = await self.validate()
            workers = await asyncio.to_thread(self.ops.workers, self.cfg["run_id"])
            self.data["workers_before"] = workers
            if not valid_workers(workers, self.cfg["worker_image"]):
                raise ValueError("T27 production/test workers must match exact published image")
            run = await self.session("transport", {"transport_case": "tls"})
            self.data["transport_completed"] = (
                run.outcome == "COMPLETED"
                and run.generation == 1
                and len(run.boundaries) == 1
                and run.boundaries[0].get("tool") == "transport_probe"
            )
            self.data["probe"] = await asyncio.to_thread(
                read_probe, self.base.artifact_dir, run.snapshot
            )
            self.data["t4"] = await BoundT4(self).t4()
            self.data["workers_after"] = await asyncio.to_thread(
                self.ops.workers, self.cfg["run_id"]
            )
            self.data["worker_stable"] = self.data["workers_after"] == workers
            self.data["fixture_stable"] = await self.validate() == fixture
            if not self.data["fixture_stable"]:
                raise ValueError("T27 fixture changed during qualification")
            self.data["cleanup_confirmed"] = len(self.data["sessions"]) == 2 and all(
                s["cleanup_confirmed"] for s in self.data["sessions"]
            )
            return evaluate(self.data)
        except Exception as exc:
            self.data["evidence_errors"].append(type(exc).__name__)
            return {
                "id": "T27",
                "result": "error",
                "error": type(exc).__name__,
                "evidence": self.data,
            }
