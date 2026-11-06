"""Opt-in isolated T24 worker; production workflow and real activity results unchanged.

Launch only on the root-created control's unique queue. Kubernetes controls are
read-only. Gates are genuine activity heartbeats, not synthetic history events.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

from temporalio import activity
from temporalio.exceptions import CancelledError
from temporalio.worker import Worker

from runner import activities as act
from runner.conformance_lifecycle_control import CASES, Control, canonical_uuid
from runner.types import BoundaryIn, BoundaryOut, GenIn, SessionInput
from runner.worker import connect
from runner.workflow import SessionWorkflow


class LifecycleActivities:
    def __init__(self, control, worker_uid):
        if not worker_uid:
            raise ValueError("lifecycle worker requires its downward-API Pod UID")
        self.control, self.worker_uid = control, worker_uid

    def case(self, inp):
        cfg = self.control.config
        case = inp.task.get("lifecycle_case") if isinstance(inp.task, dict) else None
        if (
            case not in CASES
            or set(inp.task) != {"lifecycle_case"}
            or (inp.tenant, inp.agent, inp.release) != (cfg["tenant"], cfg["agent"], cfg["release"])
            or inp.session_id != cfg["sessions"][case]
            or not inp.grant_handle
            or inp.max_boundaries != 2
            or inp.seed is not None
        ):
            raise ValueError("session is not bound to this lifecycle fixture run")
        self.control.read()
        return case

    def gate(self, inp, stage, generation):
        case = self.case(inp)
        while True:
            # The first cleanup attempt is deliberately waiting for process loss.
            # It is never given a synthetic success/failure result. Ordinary retry
            # attempts after the killed worker execute the real operation.
            if stage == "boundary1" and self.control.released(case, stage, generation):
                return
            self.control.read()
            activity.heartbeat(
                {
                    "schema": "t24-gate-v1",
                    "case": case,
                    "session_id": inp.session_id,
                    "stage": stage,
                    "generation": generation,
                    "worker_uid": self.worker_uid,
                }
            )
            if activity.is_cancelled():
                raise CancelledError("lifecycle gate cancelled")
            time.sleep(0.25)

    @activity.defn(name="preflight")
    def preflight(self, inp: SessionInput) -> None:
        self.case(inp)
        return act.preflight(inp)

    @activity.defn(name="run_boundary")
    def run_boundary(self, arg: BoundaryIn) -> BoundaryOut:
        self.case(arg.inp)
        if arg.generation == 1 and arg.boundary == 1:
            self.gate(arg.inp, "boundary1", arg.generation)
        return act.run_boundary(arg)

    @activity.defn(name="delete_sandbox")
    def delete_sandbox(self, arg: GenIn) -> str:
        case = self.case(arg.inp)
        if case == "worker_kill" and activity.info().attempt == 1:
            self.gate(arg.inp, "delete_sandbox", arg.generation)
        return act.delete_sandbox(arg)

    @activity.defn(name="delete_grant")
    def delete_grant(self, inp: SessionInput) -> str:
        case = self.case(inp)
        if case == "worker_kill" and activity.info().attempt == 1:
            self.gate(inp, "delete_grant", 1)
        return act.delete_grant(inp)

    def registered(self):
        replacements = {
            "preflight": self.preflight,
            "run_boundary": self.run_boundary,
            "delete_sandbox": self.delete_sandbox,
            "delete_grant": self.delete_grant,
        }
        return [replacements.get(fn.__name__, fn) for fn in act.SESSION_ACTIVITIES]


async def run():
    deps = act.Deps.from_cluster()
    control = Control(deps.core)
    uid = os.environ.get("POD_UID", "")
    if not canonical_uuid(uid):
        raise ValueError("lifecycle worker requires its actual Pod UID")
    if os.environ.get("LIFECYCLE_RUN_ID") != control.config["run_id"]:
        raise ValueError("worker run id differs from pinned lifecycle control")
    temporal = await connect(control.config["tenant"])
    deps.temporal = temporal
    act.configure(deps)
    wrapped = LifecycleActivities(control, uid)
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
