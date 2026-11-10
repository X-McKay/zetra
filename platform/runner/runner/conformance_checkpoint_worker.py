"""Opt-in T23 worker: unchanged SessionWorkflow, real activities, observed barriers.

Never run on a normal session queue. The operator creates the exact control
ConfigMap and temporary worker. Each kill needs the harness's gate-bound command.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from temporalio import activity
from temporalio.worker import Worker

from runner import activities as act
from runner.conformance_checkpoint_control import ConfigConflict, Control
from runner.types import BoundaryIn, SessionInput, SnapshotIn
from runner.worker import connect
from runner.workflow import SessionWorkflow

STAGES = {
    "a": ("before_exec", 1),
    "b": ("after_read_before_done", 1),
    "c": ("before_snapshot", 1),
    "d": ("during_store_copy", 1),
    "e": ("after_commit", 2),
}
PREFIX = b"checkpoint_stage "


def now():
    return datetime.now(UTC).isoformat()


class Hooks:
    def __init__(self, deps, control):
        self.deps, self.control = deps, control
        self.context = threading.local()

    def binding(self, inp):
        cfg = self.control.config
        labels = [case for case, session in cfg["sessions"].items() if session == inp.session_id]
        if len(labels) != 1:
            raise ConfigConflict("unbound checkpoint session")
        case = labels[0]
        if (
            inp.tenant != cfg["tenant"]
            or inp.agent != cfg["agent"]
            or inp.release != cfg["release"]
            or inp.grant_handle is not None
            or inp.seed is not None
            or inp.max_boundaries != 3
            or inp.task
            != {
                "checkpoint_case": "control" if case.startswith("control-") else case,
                "run_id": cfg["run_id"],
            }
        ):
            raise ConfigConflict("checkpoint session binding changed")
        inp.validate()
        return case

    def pod(self, b, expected=None):
        pod = self.deps.core.read_namespaced_pod(
            act.pod_name(b.inp.session_id, b.generation), b.inp.tenant, _request_timeout=10
        )
        labels = pod.metadata.labels or {}
        want = {
            "sandbox": "true",
            "tenant": b.inp.tenant,
            "agent": b.inp.agent,
            "release": b.inp.release,
            "session": b.inp.session_id,
            "generation": str(b.generation),
        }
        if (
            any(labels.get(act.LABEL + key) != value for key, value in want.items())
            or not pod.metadata.uid
            or (expected and pod.metadata.uid != expected)
            or pod.metadata.deletion_timestamp
            or pod.status.phase != "Running"
        ):
            raise ConfigConflict("checkpoint pod identity or liveness changed")
        return pod.metadata.uid

    def gate(self, stage, b, *, upload=None):
        case = self.binding(b.inp)
        if STAGES.get(case) != (stage, b.boundary) or b.generation != 1:
            raise ConfigConflict("checkpoint gate is not the authorized failure point")
        if self.control.get("receipt-" + case) is not None:
            raise ConfigConflict("checkpoint fault already executed")
        if stage == "during_store_copy":
            if (
                not isinstance(upload, dict)
                or set(upload) != {"copied_bytes", "candidate_key"}
                or type(upload["copied_bytes"]) is not int
                or not 0 < upload["copied_bytes"] < act.MAX_SNAPSHOT
                or not isinstance(upload["candidate_key"], str)
                or len(upload["candidate_key"]) != 64
                or any(c not in "0123456789abcdef" for c in upload["candidate_key"])
            ):
                raise ConfigConflict("upload gate lacks actual partial-copy evidence")
        elif upload is not None:
            raise ConfigConflict("upload receipt at wrong failure point")
        uid = self.pod(b)
        gate = {
            "run_id": self.control.config["run_id"],
            "gate_id": str(uuid.uuid4()),
            "case": case,
            "session_id": b.inp.session_id,
            "generation": b.generation,
            "boundary": b.boundary,
            "stage": stage,
            "pod": act.pod_name(b.inp.session_id, 1),
            "pod_uid": uid,
            "reached_at": now(),
        }
        if upload is not None:
            gate["upload"] = upload
        self.control.patch("gate", self.control.get("gate"), gate)
        deadline = time.monotonic() + 60
        while True:
            activity.heartbeat(*([{"snapshot_pod_uid": uid}] if isinstance(b, SnapshotIn) else []))
            command = self.control.get("command")
            if command and command.get("gate_id") == gate["gate_id"]:
                if command != {
                    "run_id": gate["run_id"],
                    "gate_id": gate["gate_id"],
                    "action": "kill",
                }:
                    raise ConfigConflict("checkpoint command is not exact kill authorization")
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("checkpoint gate was not released")
            time.sleep(0.1)
        self.pod(b, uid)
        started = now()
        # An ordinary API deletion with an exact UID, never name-only or force deletion.
        self.deps.core.delete_namespaced_pod(
            gate["pod"],
            b.inp.tenant,
            body=client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=uid)),
            _request_timeout=10,
        )
        deadline = time.monotonic() + 40
        while True:
            activity.heartbeat(*([{"snapshot_pod_uid": uid}] if isinstance(b, SnapshotIn) else []))
            try:
                current = self.deps.core.read_namespaced_pod(
                    gate["pod"], b.inp.tenant, _request_timeout=10
                )
            except ApiException as exc:
                if exc.status == 404:
                    break
                raise
            if current.metadata.uid != uid:
                raise ConfigConflict("replacement appeared before confirmed deletion")
            if time.monotonic() >= deadline:
                raise TimeoutError("checkpoint pod deletion was not confirmed")
            time.sleep(0.1)
        receipt = {**gate, "delete_started_at": started, "deleted_at": now(), "deleted": True}
        self.control.patch("receipt-" + case, None, receipt)
        return receipt

    def stage(self, b, event):
        case = self.binding(b.inp)
        expected = {
            "run_id": self.control.config["run_id"],
            "session_id": b.inp.session_id,
            "generation": b.generation,
            "boundary": b.boundary,
            "case": "control" if case.startswith("control-") else case,
        }
        if (
            not isinstance(event, dict)
            or set(event) != {*expected, "stage", "time"}
            or any(event.get(k) != v for k, v in expected.items())
            or event.get("stage") not in {"boundary_start", "read_complete", "done"}
        ):
            raise ConfigConflict("checkpoint fixture stage is not bound to the activity")
        stamp = datetime.fromisoformat(event["time"])
        if stamp.tzinfo is None:
            raise ConfigConflict("checkpoint fixture stage has no timezone")
        # Fixed metadata only; arbitrary guest stderr and MCP result bodies are never logged.
        logging.getLogger(__name__).info(
            json.dumps(
                {
                    "event": "checkpoint_stage",
                    **event,
                    "pod": act.pod_name(b.inp.session_id, b.generation),
                    "observed_at": now(),
                },
                sort_keys=True,
            )
        )
        if (
            case == "b"
            and b.generation == 1
            and b.boundary == 1
            and event["stage"] == "read_complete"
        ):
            self.gate("after_read_before_done", b)

    @activity.defn(name="preflight")
    def preflight(self, inp: SessionInput) -> int | None:
        self.binding(inp)
        return act.preflight(inp)

    @activity.defn(name="run_boundary")
    def run_boundary(self, b: BoundaryIn):
        case = self.binding(b.inp)
        self.context.boundary = b
        try:
            if b.generation == 1 and (case, b.boundary) in {("a", 1), ("e", 2)}:
                self.gate("before_exec" if case == "a" else "after_commit", b)
            return act.run_boundary(b)
        finally:
            self.context.boundary = None

    @activity.defn(name="snapshot_workspace")
    def snapshot_workspace(self, s: SnapshotIn):
        case = self.binding(s.inp)
        self.context.snapshot = s
        try:
            if case == "c" and s.generation == 1 and s.boundary == 1:
                self.gate("before_snapshot", s)
            return act.snapshot_workspace(s)
        finally:
            self.context.snapshot = None

    def install(self):
        hooks = self
        store = self.deps.store

        class Store:
            def open(self, key):
                return store.open(key)

            def put(self, key, src):
                s = getattr(hooks.context, "snapshot", None)
                target = s and hooks.binding(s.inp) == "d" and (s.generation, s.boundary) == (1, 1)
                if not target:
                    return store.put(key, src)

                class Source:
                    copied = 0
                    held = False

                    def read(self, size):
                        # The store already wrote the prior chunk before asking for more.
                        if self.copied and not self.held:
                            self.held = True
                            hooks.gate(
                                "during_store_copy",
                                s,
                                upload={"copied_bytes": self.copied, "candidate_key": key},
                            )
                        chunk = src.read(size)
                        self.copied += len(chunk)
                        return chunk

                return store.put(key, Source())

        def stream(*args, **kwargs):
            ws = act._stream_nocapture(*args, **kwargs)
            b = getattr(hooks.context, "boundary", None)
            if b is None:
                return ws

            class Socket:
                buffer = b""

                def __getattr__(self, name):
                    return getattr(ws, name)

                def read_stderr(self):
                    chunk = act._bytes(ws.read_stderr())
                    self.buffer += chunk
                    if len(self.buffer) > 4096:
                        raise ConfigConflict("checkpoint stage stream exceeds bound")
                    while b"\n" in self.buffer:
                        line, self.buffer = self.buffer.split(b"\n", 1)
                        if line.startswith(PREFIX):
                            hooks.stage(b, json.loads(line[len(PREFIX) :]))
                    return chunk

            return Socket()

        self.deps.store = Store()
        self.deps.exec = functools.partial(act.kube_exec, self.deps.core, stream_fn=stream)
        act.configure(self.deps)
        replacements = {
            act.preflight: self.preflight,
            act.run_boundary: self.run_boundary,
            act.snapshot_workspace: self.snapshot_workspace,
        }
        return [replacements.get(fn, fn) for fn in act.SESSION_ACTIVITIES]


async def run():
    deps = act.Deps.from_cluster()
    control = Control(deps.core)
    hooks = Hooks(deps, control)
    activities = hooks.install()
    temporal = await connect(control.config["tenant"])
    # No ordinary queue, Schedule, or workflow implementation is changed.
    with ThreadPoolExecutor(max_workers=8) as executor:
        await Worker(
            temporal,
            task_queue=control.config["queue"],
            workflows=[SessionWorkflow],
            activities=activities,
            activity_executor=executor,
            max_concurrent_activities=8,
        ).run()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-checkpoint-faults", action="store_true", required=True)
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
