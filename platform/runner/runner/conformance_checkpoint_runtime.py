"""Agent-platform T23 orchestration. Only the exact control ConfigMap is patched."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from datetime import timedelta

from temporalio.common import WorkflowIDReusePolicy

from runner.conformance_admission import AdmissionSessions, KubernetesAdmission, ready, settled
from runner.conformance_checkpoint import checkpoint_state, evaluate_case, read_evidence
from runner.conformance_checkpoint_control import CASES, Control, ConfigConflict
from runner.conformance_checkpoint_worker import STAGES
from runner.conformance_protocol import decode_history
from runner.types import SessionInput
from runner.workflow import SessionWorkflow


class KubernetesCheckpoint(KubernetesAdmission):
    def workers(self, run_id):
        found = self.core.list_namespaced_pod(
            "agent-platform",
            label_selector="app.kubernetes.io/name=session-runner,agentplatform.io/tenant=tenant-test",
            _request_timeout=10,
        ).items
        rows = []
        for pod in found:
            containers = [c for c in pod.status.container_statuses or [] if c.name == "runner"]
            specs = [c for c in pod.spec.containers if c.name == "runner"]
            status = containers[0] if len(containers) == 1 else None
            rows.append(
                {
                    "name": pod.metadata.name,
                    "uid": pod.metadata.uid,
                    "test_worker": (pod.metadata.labels or {}).get(
                        "agentplatform.io/checkpoint-run"
                    )
                    == run_id,
                    "ready": bool(
                        status
                        and status.ready
                        and pod.status.phase == "Running"
                        and not pod.metadata.deletion_timestamp
                    ),
                    "restart_count": status.restart_count if status else None,
                    "image_id": status.image_id if status else None,
                    "image": specs[0].image if len(specs) == 1 else None,
                }
            )
        return sorted(rows, key=lambda x: x["name"])

    def worker_logs(self, worker, since):
        current = self.core.read_namespaced_pod(
            worker["name"], "agent-platform", _request_timeout=10
        )
        if current.metadata.uid != worker["uid"]:
            raise ConfigConflict("checkpoint worker was replaced")
        return self.core.read_namespaced_pod_log(
            worker["name"],
            "agent-platform",
            container="runner",
            since_seconds=int(time.time() - since) + 10,
            _request_timeout=10,
        )


def pinned_workers(rows):
    test = [r for r in rows if r["test_worker"]]
    production = [r for r in rows if not r["test_worker"]]
    return (
        len(test) == 1
        and bool(production)
        and all(r["ready"] for r in rows)
        and test[0]["restart_count"] == 0
        and len({r["image"] for r in rows}) == 1
        and len({r["image_id"] for r in rows}) == 1
        and all(r.get("image") and "@sha256:" in r["image"] and r.get("image_id") for r in rows)
    )


def gate_ready(case, history, stages, reads, gate):
    """Observe the selected real activity phase before authorizing its exact Pod kill."""
    _, boundary = STAGES[case]
    runs = [
        h
        for h in history
        if h["activity"] == "run_boundary" and h["generation"] == 1 and h["boundary"] == boundary
    ]
    if len(runs) != 1:
        return False
    current = runs[0]
    observed = {
        s.get("stage") for s in stages if s.get("generation") == 1 and s.get("boundary") == 1
    }
    if case == "a":
        return current.get("completed") is None and not observed and not reads
    if (
        len(reads) != 1
        or reads[0].get("generation") != 1
        or reads[0].get("tool") != "pods_list_in_namespace"
        or reads[0].get("reason") != "allow"
        or reads[0].get("gateway_count") != 1
        or reads[0].get("capture_count") != 1
        or str(reads[0].get("gateway_status")) != "200"
        or reads[0].get("capture_status") != 200
        or reads[0].get("capture_bound") is not True
        or "read_complete" not in observed
    ):
        return False
    if case == "b":
        return current.get("completed") is None and "done" not in observed
    snapshots = [
        h
        for h in history
        if h["activity"] == "snapshot_workspace" and h["generation"] == 1 and h["boundary"] == 1
    ]
    if len(snapshots) != 1 or "done" not in observed:
        return False
    snapshot = snapshots[0]
    if case == "e":
        return (
            current.get("completed") is None
            and isinstance(snapshot.get("result"), str)
            and snapshot.get("completed") is not None
            and snapshot["completed"] < current["scheduled"]
        )
    running_snapshot = (
        isinstance(current.get("result"), dict)
        and current["result"].get("boundary") == 1
        and current["result"].get("done") is False
        and current.get("completed") is not None
        and current["completed"] < snapshot["scheduled"]
        and snapshot.get("completed") is None
    )
    if case == "d":
        upload = gate.get("upload") or {}
        return (
            running_snapshot
            and type(upload.get("copied_bytes")) is int
            and upload["copied_bytes"] > 0
        )
    return running_snapshot


class CheckpointHarness:
    def __init__(
        self, ops, temporal, base, release, artifact_dir, *, allow_mutations=False, timeout=300
    ):
        self.ops, self.temporal, self.base = ops, temporal, base
        self.release, self.artifact_dir = release, artifact_dir
        self.allow_mutations, self.timeout = allow_mutations, timeout
        self.cleanup = AdmissionSessions(temporal, ops, timeout=180)

    async def history(self, handle):
        return await decode_history(
            [e async for e in handle.fetch_history_events()], self.temporal.data_converter
        )

    async def logs(self, session):
        from runner.conformance import (
            parse_aps_log,
            parse_capture_log,
            parse_gateway_log,
            LOG_UNAVAILABLE,
        )

        since = max(1, time.time() - self.started + 10)
        raw = await asyncio.gather(
            self.base.aps_logs(since),
            self.base.gateway_logs(since),
            self.base.capture_logs(since),
            asyncio.to_thread(self.ops.worker_logs, self.worker, self.started),
        )
        aps, captures, gateway = (
            parse_aps_log(raw[0]),
            parse_capture_log(raw[2]),
            parse_gateway_log(raw[1]),
        )
        stages = []
        for line in raw[3].splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (
                isinstance(row, dict)
                and row.get("event") == "checkpoint_stage"
                and row.get("session_id") == session
            ):
                stages.append(row)
        return {
            **read_evidence(session, aps, gateway, captures),
            "stages": stages,
            "logs_readable": all(LOG_UNAVAILABLE not in value for value in raw),
        }

    async def release_gate(self, case, data, handle):
        gate = await asyncio.to_thread(self.control.get, "gate")
        if not gate or gate.get("session_id") != data["session_id"]:
            return False
        stage, boundary = STAGES[case]
        required = {
            "run_id": self.cfg["run_id"],
            "case": case,
            "generation": 1,
            "boundary": boundary,
            "stage": stage,
        }
        if any(gate.get(k) != v for k, v in required.items()) or not gate.get("gate_id"):
            raise ConfigConflict("checkpoint gate has unexpected binding")
        pods = await asyncio.to_thread(self.ops.pods, data["session_id"])
        if (
            len(pods) != 1
            or pods[0]["metadata"]["uid"] != gate.get("pod_uid")
            or pods[0]["metadata"]["name"] != gate.get("pod")
        ):
            raise ConfigConflict("checkpoint gate pod identity changed")
        history = await self.history(handle)
        samples = await self.logs(data["session_id"])
        # Retain partial evidence on timeout too; missing correlation must not look
        # like a missing gate/history when the operator inspects a failed report.
        data.update(
            gate=gate, gate_history=history, gate_stages=samples["stages"], gate_observation=samples
        )
        if not samples["logs_readable"] or not samples["read_coverage"]["complete"]:
            return False
        if not gate_ready(case, history, samples["stages"], samples["reads"], gate):
            return False
        # The worker only advertises observed points. Keep the real in-flight history
        # and stage records before issuing the exact gate-bound kill command.
        command = await asyncio.to_thread(self.control.get, "command")
        await asyncio.to_thread(
            self.control.patch,
            "command",
            command,
            {"run_id": self.cfg["run_id"], "gate_id": gate["gate_id"], "action": "kill"},
        )
        return True

    async def session(self, case, data):
        cfg = self.cfg
        inp = SessionInput(
            cfg["tenant"],
            cfg["agent"],
            cfg["release"],
            cfg["sessions"][case],
            None,
            {
                "checkpoint_case": "control" if case.startswith("control-") else case,
                "run_id": cfg["run_id"],
            },
            max_boundaries=3,
        )
        inp.validate()
        data.update(session_id=inp.session_id, run_id=cfg["run_id"])
        handle = await self.temporal.start_workflow(
            SessionWorkflow.run,
            inp,
            id=inp.workflow_id,
            task_queue=cfg["queue"],
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            execution_timeout=timedelta(minutes=10),
            task_timeout=timedelta(seconds=10),
        )
        result_task = asyncio.create_task(handle.result())
        try:
            deadline = time.monotonic() + self.timeout
            if not case.startswith("control-"):
                released = False
                while not result_task.done() and time.monotonic() < deadline:
                    if await self.release_gate(case, data, handle):
                        released = True
                        break
                    await asyncio.sleep(0.1)
                if not released:
                    raise TimeoutError("checkpoint failure gate was not observed")
            result = await asyncio.wait_for(
                asyncio.shield(result_task), max(1, deadline - time.monotonic())
            )
            data.update(asdict(result))
            data["history"] = await self.history(handle)
            data["receipt"] = await asyncio.to_thread(self.control.get, "receipt-" + case)
            data["pods"] = [
                p["metadata"]["name"]
                for p in await asyncio.to_thread(self.ops.pods, inp.session_id)
            ]
            keys = [
                h["result"]
                for h in data["history"]
                if h["activity"] == "snapshot_workspace" and isinstance(h.get("result"), str)
            ]
            data["checkpoints"] = {
                key: await asyncio.to_thread(checkpoint_state, self.artifact_dir, key)
                for key in keys
            }
            if case == "d" and data["receipt"] and data["receipt"].get("upload"):
                data["candidate"] = await asyncio.to_thread(
                    checkpoint_state, self.artifact_dir, data["receipt"]["upload"]["candidate_key"]
                )
            # A bounded log delay may supply missing records. Actual repeated/misordered
            # evidence is retained and never retried as requests to manufacture a pass.
            for attempt in range(30):
                data.update(await self.logs(inp.session_id))
                verdict = evaluate_case(case, data)
                if verdict["result"] == "pass" or attempt == 29:
                    return verdict
                await asyncio.sleep(1)
        finally:
            await settled(self.cleanup._close(inp, handle, result_task))
            data["cleanup_confirmed"] = not await asyncio.to_thread(self.ops.pods, inp.session_id)

    async def t23(self):
        cases = []
        try:
            if not self.allow_mutations or self.ops.tenant != "tenant-test" or self.timeout < 300:
                raise ValueError(
                    "T23 requires explicit checkpoint faults in tenant-test and timeout>=300"
                )
            self.control = await asyncio.to_thread(Control, self.ops.core)
            self.cfg = self.control.config
            if self.release != self.cfg["release"]:
                raise ConfigConflict("checkpoint release differs from the operator binding")
            release = await asyncio.to_thread(self.ops.release, self.release)
            if (
                not ready(release)
                or release["spec"]["agent"] != "conformance-checkpoint"
                or "@sha256:" not in release["spec"]["image"]
            ):
                raise ValueError("checkpoint fixture must be signed, Ready and immutable")
            before = await asyncio.to_thread(self.ops.workers, self.cfg["run_id"])
            if not pinned_workers(before):
                raise ValueError(
                    "exact temporary worker and production workers must share one Ready pinned image"
                )
            self.worker = next(w for w in before if w["test_worker"])
            self.started = time.time()
            for case in CASES:
                data = {}
                row = {"case": case, "result": "error", "evidence": data}
                cases.append(row)
                row.update(await self.session(case, data))
                if row["result"] != "pass":
                    break
            after = await asyncio.to_thread(self.ops.workers, self.cfg["run_id"])
            final_release = await asyncio.to_thread(self.ops.release, self.release)
            checks = {
                "all_five_faults_and_two_controls": len(cases) == 7,
                "all_evidence_gates": all(r["result"] == "pass" for r in cases),
                "cleanup_confirmed_each_session": all(
                    r["evidence"].get("cleanup_confirmed") is True for r in cases
                ),
                "same_pinned_worker_throughout": before == after and pinned_workers(after),
                "release_unchanged_ready": release["metadata"]["uid"]
                == final_release["metadata"]["uid"]
                and release["spec"] == final_release["spec"]
                and ready(final_release),
            }
            return {
                "id": "T23",
                "name": "checkpoint failure points",
                "cases": cases,
                "result": "pass" if all(checks.values()) else "fail",
                "checks": checks,
                "fixture_image": release["spec"]["image"],
                "workers": before,
                "run_id": self.cfg["run_id"],
            }
        except Exception as exc:
            return {
                "id": "T23",
                "name": "checkpoint failure points",
                "result": "error",
                "cases": cases,
                "error": type(exc).__name__,
            }
