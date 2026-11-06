"""Isolated T21 worker. Real activities/workflow; root-bound fault commands only."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from temporalio import activity
from temporalio.worker import Worker

from runner import activities as act
from runner.conformance_generation_control import CASES, FINALIZER, Control
from runner.conformance_lifecycle_control import canonical_uuid
from runner.types import BoundaryIn, SessionInput
from runner.worker import connect
from runner.workflow import SessionWorkflow


def marker(session):
    return hashlib.sha256(session.encode()).hexdigest()[:16]


class GenerationActivities:
    def __init__(self, deps, control, worker_uid):
        if not canonical_uuid(worker_uid):
            raise ValueError("T21 requires downward-API worker UID")
        self.deps, self.control, self.worker_uid = deps, control, worker_uid

    def case(self, inp):
        cfg = self.control.config
        cases = [case for case in CASES if cfg["sessions"][case] == inp.session_id]
        if len(cases) != 1:
            raise ValueError("unbound T21 session")
        case = cases[0]
        expected = (
            (
                "conformance-b",
                cfg["probe_release"],
                {
                    "tool": "probe_revocation",
                    "args": {"marker": marker(inp.session_id), "duration": "180"},
                },
                8,
            )
            if case == "fence_only"
            else (
                "conformance-checkpoint",
                cfg["recovery_release"],
                {"checkpoint_case": "a", "run_id": cfg["run_id"]},
                3,
            )
        )
        if (
            inp.tenant != "tenant-test"
            or (inp.agent, inp.release, inp.task, inp.max_boundaries) != expected
            or inp.grant_handle is not None
            or inp.seed is not None
        ):
            raise ValueError("T21 fixture input differs")
        inp.validate()
        self.control.read()
        return case

    def pod(self, arg):
        pod = self.control.core.read_namespaced_pod(
            act.pod_name(arg.inp.session_id, 1), arg.inp.tenant, _request_timeout=5
        )
        want = {
            "sandbox": "true",
            "tenant": arg.inp.tenant,
            "agent": arg.inp.agent,
            "release": arg.inp.release,
            "session": arg.inp.session_id,
            "generation": "1",
        }
        if (
            not pod.metadata.uid
            or pod.metadata.deletion_timestamp
            or pod.status.phase != "Running"
            or any(
                (pod.metadata.labels or {}).get(act.LABEL + key) != value
                for key, value in want.items()
            )
        ):
            raise ValueError("T21 requires exact Running generation 1")
        return pod

    def record(self, case, arg, uid, operation, **metadata):
        logging.getLogger(__name__).info(
            json.dumps(
                {
                    "event": "t21_worker_receipt",
                    "run_id": self.control.config["run_id"],
                    "case": case,
                    "session_id": arg.inp.session_id,
                    "worker_uid": self.worker_uid,
                    "pod_uid": uid,
                    "operation": operation,
                    **metadata,
                },
                sort_keys=True,
            )
        )

    def delete_for_recovery(self, case, arg):
        pod = self.pod(arg)
        deadline = time.monotonic() + 120
        while not self.control.commanded(case, pod.metadata.uid, self.worker_uid):
            activity.heartbeat(
                {
                    "schema": "t21-gate-v1",
                    "case": case,
                    "run_id": self.control.config["run_id"],
                    "session_id": arg.inp.session_id,
                    "pod_uid": pod.metadata.uid,
                    "worker_uid": self.worker_uid,
                }
            )
            if activity.is_cancelled() or time.monotonic() >= deadline:
                raise TimeoutError("T21 root command gate ended")
            time.sleep(0.25)
        current = self.pod(arg)
        finalizers = current.metadata.finalizers or []
        if (
            current.metadata.uid != pod.metadata.uid
            or (case == "blocked_deletion" and finalizers != [FINALIZER])
            or (case == "recovery_control" and finalizers)
        ):
            raise ValueError("T21 UID/finalizer precondition differs")
        uid = pod.metadata.uid
        self.deps.core.delete_namespaced_pod(
            pod.metadata.name,
            arg.inp.tenant,
            body=client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=uid)),
            _request_timeout=5,
        )
        deadline = time.monotonic() + 45
        while True:
            activity.heartbeat()
            try:
                current = self.deps.core.read_namespaced_pod(
                    pod.metadata.name, arg.inp.tenant, _request_timeout=5
                )
            except ApiException as exc:
                if exc.status != 404:
                    raise
                if case != "recovery_control":
                    raise ValueError("finalizer did not hold generation 1") from None
                observed = "404"
                break
            if current.metadata.uid != uid:
                raise ValueError("T21 target replaced before confirmed deletion")
            statuses = current.status.container_statuses or []
            if (
                case == "blocked_deletion"
                and current.metadata.deletion_timestamp
                and FINALIZER in (current.metadata.finalizers or [])
                and statuses
                and all(s.state.terminated for s in statuses)
            ):
                observed = "terminated_and_held"
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("T21 actual Pod termination/deletion not observed")
            time.sleep(0.25)
        self.record(case, arg, uid, "delete_for_recovery", acknowledged=True, observed=observed)
        # Caller invokes real production exec on the terminated/absent Pod.

    def fence_monitor(self, arg, stop):
        pod = self.pod(arg)
        while not stop.wait(0.1):
            self.control.read()
            if not self.control.commanded("fence_only", pod.metadata.uid, self.worker_uid):
                continue
            if self.pod(arg).metadata.uid != pod.metadata.uid:
                raise ValueError("T21 fence target changed")
            response = self.deps.aps.put(
                "/v1/sessions/" + arg.inp.session_id + "/generation",
                {"generation": 2, "tenant": arg.inp.tenant, "agent": arg.inp.agent},
            )
            if response != {"session_id": arg.inp.session_id, "generation": 2}:
                raise ValueError("APS did not acknowledge exact T21 fence")
            self.record(
                "fence_only",
                arg,
                pod.metadata.uid,
                "fence_generation_2",
                generation=2,
                acknowledged=True,
            )
            return

    @activity.defn(name="preflight")
    def preflight(self, inp: SessionInput):
        self.case(inp)
        return act.preflight(inp)

    @activity.defn(name="run_boundary")
    def run_boundary(self, arg: BoundaryIn):
        case = self.case(arg.inp)
        if arg.generation == 1 and case != "fence_only" and arg.boundary == 1:
            self.delete_for_recovery(case, arg)
        if case != "fence_only" or arg.generation != 1:
            return act.run_boundary(arg)
        stop, errors = threading.Event(), []

        def monitor():
            try:
                self.fence_monitor(arg, stop)
            except Exception as exc:
                errors.append(exc)
                self.record(case, arg, "", "fence_error", error_type=type(exc).__name__)

        thread = threading.Thread(target=monitor, name="t21-fence-monitor")
        thread.start()
        original_error = None
        try:
            result = act.run_boundary(arg)
        except BaseException as exc:
            original_error = exc
        finally:
            stop.set()
            # Read-only client retries are disabled below, every read is <=5s,
            # and APS PUT is <=10s. Settle the real operation before returning;
            # never leave an unknown fence mutation in a background thread.
            thread.join()
        if original_error is not None:
            raise original_error
        if errors:
            raise errors[0]
        return result

    def registered(self):
        replacements = {"preflight": self.preflight, "run_boundary": self.run_boundary}
        return [replacements.get(fn.__name__, fn) for fn in act.SESSION_ACTIVITIES]


async def run():
    deps = act.Deps.from_cluster()
    observer_config = client.Configuration.get_default_copy()
    observer_config.retries = 0
    control = Control(client.CoreV1Api(client.ApiClient(observer_config)))
    if os.environ.get("GENERATION_RUN_ID") != control.config["run_id"]:
        raise ValueError("T21 worker run identity mismatch")
    temporal = await connect("tenant-test")
    deps.temporal = temporal
    act.configure(deps)
    wrapped = GenerationActivities(deps, control, os.environ.get("POD_UID", ""))
    with ThreadPoolExecutor(max_workers=8) as executor:
        async with Worker(
            temporal,
            task_queue=control.config["queue"],
            workflows=[SessionWorkflow],
            activities=wrapped.registered(),
            activity_executor=executor,
        ):
            await asyncio.Event().wait()


def main():
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
