"""Isolated T27 pre-exec gate; no production worker option or synthetic result."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from kubernetes import client
from temporalio import activity
from temporalio.exceptions import CancelledError
from temporalio.worker import Worker

from runner import activities as act
from runner.conformance_transport_control import (
    Control,
    canonical_uuid,
    capture_started,
    validate_gate,
)
from runner.types import BoundaryIn, BoundaryOut, SessionInput
from runner.worker import connect
from runner.workflow import SessionWorkflow


class TransportActivities:
    def __init__(self, control, core, worker_uid):
        if not canonical_uuid(worker_uid):
            raise ValueError("transport worker requires its downward-API Pod UID")
        self.control, self.core, self.worker_uid = control, core, worker_uid
        self.gates = {}

    def case(self, inp):
        cfg = self.control.config
        for name, agent in (("transport", "conformance-transport"), ("t4", "conformance")):
            if inp.session_id == cfg["sessions"][name]:
                if (
                    (inp.tenant, inp.agent, inp.release)
                    != (
                        cfg["tenant"],
                        agent,
                        cfg["transport_release" if name == "transport" else "t4_release"],
                    )
                    or inp.grant_handle is not None
                    or inp.seed is not None
                ):
                    break
                if name == "transport" and inp.task != {"transport_case": "tls"}:
                    break
                self.control.read()
                return name
        raise ValueError("session is not one of the two exact bound transport cases")

    def pod(self, inp):
        value = self.core.read_namespaced_pod(
            act.pod_name(inp.session_id, 1), inp.tenant, _request_timeout=5
        )
        pod = (
            value
            if isinstance(value, dict)
            else client.ApiClient().sanitize_for_serialization(value)
        )
        labels = pod.get("metadata", {}).get("labels", {})
        if (
            pod.get("metadata", {}).get("deletionTimestamp")
            or pod.get("status", {}).get("phase") != "Running"
            or pod.get("spec", {}).get("runtimeClassName") != "kata-qemu-runtime-rs"
            or any(
                labels.get("agentplatform.io/" + key) != expected
                for key, expected in {
                    "session": inp.session_id,
                    "agent": "conformance",
                    "generation": "1",
                }.items()
            )
        ):
            raise ValueError("transport gate requires exact Running T4 sandbox")
        return {
            "pod": pod["metadata"]["name"],
            "pod_uid": pod["metadata"]["uid"],
            "pod_ip": pod["status"]["podIP"],
            "node": pod["spec"]["nodeName"],
        }

    def gate(self, arg):
        pod = self.pod(arg.inp)
        gate = validate_gate(
            {
                **pod,
                "run_id": self.control.config["run_id"],
                "session_id": arg.inp.session_id,
                "gate_id": str(uuid.uuid4()),
                "worker_uid": self.worker_uid,
            }
        )
        self.gates[arg.inp.session_id] = gate
        since, until = time.time(), time.monotonic() + 240
        while time.monotonic() < until:
            if self.pod(arg.inp) != pod:
                raise ValueError("T4 sandbox changed while waiting for capture")
            receipt = self.control.get("capture-start")
            if capture_started(receipt, gate):
                if (
                    not since <= receipt["started_at"] <= time.time()
                    or time.time() - receipt["started_at"] > 30
                ):
                    raise ValueError("capture-start receipt is stale or predates this gate")
                return
            activity.heartbeat(
                {"schema": "t27-gate-v1", "phase": "before_exec", "observed_at": since, **gate}
            )
            if activity.is_cancelled():
                raise CancelledError("transport capture gate cancelled")
            time.sleep(0.25)
        raise TimeoutError("operator capture was not started for exact T4 gate")

    def finish_capture(self, arg):
        gate = self.gates.get(arg.inp.session_id)
        if gate is None:
            raise ValueError("T4 capture gate is not held by this exact worker")
        start = self.control.get("capture-start")
        capture_started(start, gate)
        since, until = time.time(), time.monotonic() + 120
        while time.monotonic() < until:
            pod = self.pod(arg.inp)
            if any(pod.get(k) != gate[k] for k in ("pod", "pod_uid", "pod_ip", "node")):
                raise ValueError("captured Pod changed before capture stopped")
            activity.heartbeat(
                {"schema": "t27-gate-v1", "phase": "after_exec", "observed_at": since, **gate}
            )
            complete = self.control.get("capture-complete")
            if complete is not None:
                if (
                    complete.get("capture_start") != start
                    or not isinstance(complete.get("ended_at"), (int, float))
                    or complete["ended_at"] < since
                ):
                    raise ValueError("capture completion is not bound to this exact start")
                # A failed capture still releases ordinary cleanup. Only the
                # independent harness evaluator can issue a conformance verdict.
                return
            if activity.is_cancelled():
                raise CancelledError("transport completion gate cancelled")
            time.sleep(0.25)
        raise TimeoutError("operator capture completion missing")

    @activity.defn(name="preflight")
    def preflight(self, inp: SessionInput) -> None:
        self.case(inp)
        return act.preflight(inp)

    @activity.defn(name="run_boundary")
    def run_boundary(self, arg: BoundaryIn) -> BoundaryOut:
        case = self.case(arg.inp)
        if arg.generation != 1:
            raise ValueError("transport qualification must not switch captured generation")
        if case == "t4" and arg.boundary == 0:
            self.gate(arg)
        result = act.run_boundary(arg)
        if case == "t4" and result.done:
            self.finish_capture(arg)
        return result

    def registered(self):
        replacements = {"preflight": self.preflight, "run_boundary": self.run_boundary}
        return [replacements.get(fn.__name__, fn) for fn in act.SESSION_ACTIVITIES]


async def run():
    deps = act.Deps.from_cluster()
    control = Control(deps.core)
    if os.environ.get("TRANSPORT_RUN_ID") != control.config["run_id"]:
        raise ValueError("transport worker run identity mismatch")
    wrapped = TransportActivities(control, deps.core, os.environ.get("POD_UID", ""))
    temporal = await connect(control.config["tenant"])
    deps.temporal = temporal
    act.configure(deps)
    with ThreadPoolExecutor(max_workers=16) as executor:
        async with Worker(
            temporal,
            task_queue=control.config["queue"],
            workflows=[SessionWorkflow],
            activities=wrapped.registered(),
            activity_executor=executor,
            max_concurrent_activities=16,
        ):
            await asyncio.Event().wait()


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
