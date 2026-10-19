"""Opt-in measurements, not timing thresholds or production qualification.

BRIDGE_LATENCY=1 uv run python scripts/kind_test.py benchmarks/test_latency.py -s
"""

import asyncio
import json
import os
import secrets
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from openshell import SandboxClient, TlsConfig
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from tests.fixtures.sandbox import OpenShellSession
from zetra._runtime.activities import Activities
from zetra._runtime.artifacts import FileStore, Tape
from zetra._runtime.openshell import PYTHON
from zetra._runtime.temporal import SessionWorkflow
from zetra._runtime.vllm import MANIFEST
from zetra._runtime.workspace import Workspace

PROBE = Path(__file__).with_name("sandbox_probe.py").read_text()


class TimedSession(OpenShellSession):
    """Host-side observer; invokes the unchanged production adapter."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.samples = []
        self.phase = None
        self.cursor = None

    async def call(self, action, command):
        self.phase, self.cursor = action, command["position"]["cursor"]
        return await super().call(action, command)

    async def checked_exec(self, binding, mode, data):
        start = time.perf_counter()
        result = await super().checked_exec(binding, mode, data)
        self.samples.append(
            {
                "phase": self.phase,
                "cursor": self.cursor,
                "mode": mode,
                "seconds": time.perf_counter() - start,
                "request_bytes": len(data),
                "response_bytes": len(result),
            }
        )
        return result


def activity_timings(history):
    scheduled, started, rows = {}, {}, []
    for event in history.events:
        timestamp = event.event_time.ToDatetime().timestamp()
        if event.HasField("activity_task_scheduled_event_attributes"):
            a = event.activity_task_scheduled_event_attributes
            scheduled[event.event_id] = (timestamp, a.activity_type.name)
        elif event.HasField("activity_task_started_event_attributes"):
            a = event.activity_task_started_event_attributes
            assert a.attempt == 1, "retried run is not a clean latency sample"
            started[a.scheduled_event_id] = timestamp
        elif event.HasField("activity_task_completed_event_attributes"):
            key = event.activity_task_completed_event_attributes.scheduled_event_id
            queued, name = scheduled[key]
            rows.append(
                {
                    "name": name,
                    "scheduled": queued,
                    "started": started[key],
                    "completed": timestamp,
                    "queue_seconds": started[key] - queued,
                    "seconds": timestamp - started[key],
                }
            )
    assert len(rows) == len(scheduled) == 13
    return rows


@pytest.mark.cluster
async def test_latency_profile(tmp_path):
    if os.environ.get("BRIDGE_LATENCY") != "1":
        pytest.skip("opt-in benchmark sends synthetic live inference")
    tls = Path(os.environ["OPENSHELL_TLS_DIR"])
    client = SandboxClient(
        os.environ["OPENSHELL_ADDRESS"],
        tls=TlsConfig(
            ca_path=tls / "ca.crt",
            cert_path=tls / "tls.crt",
            key_path=tls / "tls.key",
        ),
    )
    temporal = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    output = Path(os.environ.get("BRIDGE_LATENCY_OUTPUT", ".state/latency.json"))
    result = {"durable": [], "native": [], "reconstruction": []}

    def save():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2))

    def session(provider, label):
        session_id = "latency-" + uuid.uuid4().hex
        root = tmp_path / label
        root.mkdir()
        base = root / "base"
        base.mkdir()
        (base / "README.md").write_text("Synthetic repository for latency measurement.")
        store = FileStore(root / "objects")
        manifest = MANIFEST if provider == "vllm" else "demo-v1"
        tape = Tape(store, manifest=manifest)
        remote = TimedSession(
            client,
            store,
            session=session_id,
            manifest=manifest,
            key=secrets.token_bytes(32),
            provider=provider,
        )
        start = {
            "session": session_id,
            "message": "Inspect this repository and write report.md.",
            "position": tape.empty(Workspace(base, store).capture()).model_dump(),
        }
        return remote, tape, start

    try:
        for index, provider in enumerate(["scripted"] * 3 + ["vllm"]):
            remote, tape, start = session(provider, f"durable-{index}")
            activities = Activities(remote, remote, tape)
            begin = time.perf_counter()
            try:
                async with Worker(
                    temporal,
                    task_queue=start["session"],
                    workflows=[SessionWorkflow],
                    activities=activities.registered(),
                ):
                    handle = await temporal.start_workflow(
                        SessionWorkflow.run,
                        start,
                        id=start["session"],
                        task_queue=start["session"],
                        execution_timeout=timedelta(minutes=5),
                    )
                    outcome = await handle.result()
                wall = time.perf_counter() - begin
                assert outcome["status"] == "SUCCEEDED", outcome
                assert outcome["cleanup"] == "DELETED"
                assert outcome["position"]["cursor"] == 5
                history = await handle.fetch_history()
                await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)
                rows = activity_timings(history)
                active = [
                    r
                    for r in rows
                    if r["name"] in ("advance_runner", "execute_model", "execute_tool")
                ]
                result["durable"].append(
                    {
                        "provider": provider,
                        "workflow_id": start["session"],
                        "wall_seconds": wall,
                        "active_seconds": active[-1]["completed"] - active[0]["scheduled"],
                        "workflow_to_answer_seconds": active[-1]["completed"]
                        - history.events[0].event_time.ToDatetime().timestamp(),
                        "activities": rows,
                        "rpc": remote.samples,
                    }
                )
                save()
                print(f"completed durable {provider} sample {index}", flush=True)
            finally:
                await remote.close({"start": start, "binding": remote.binding})

        # Separate baseline sandbox: same image/policy, ordinary ADK and tool calls.
        # Provision/deletion excluded from native exec measurements, as from active_seconds.
        remote, _, start = session("vllm", "baseline")
        try:
            await remote.provision(start)

            async def probe(*args):
                begin = time.perf_counter()
                response = await asyncio.to_thread(
                    client.exec,
                    remote.binding["name"],
                    [PYTHON, "-c", PROBE, *args],
                    workspace="default",
                    timeout_seconds=120,
                    no_login_shell=True,
                )
                wall = time.perf_counter() - begin
                assert response.exit_code == 0, response.stderr
                return {**json.loads(response.stdout), "exec_seconds": wall}

            for provider in ["scripted"] * 3 + ["vllm"]:
                result["native"].append(dict(provider=provider, **await probe("native", provider)))
                save()
            result["prepared"] = await probe("prepare")
            for cursor in [0, 1, 8, 32, 63, 64]:
                for _ in range(3):
                    result["reconstruction"].append(await probe("prefix", str(cursor)))
                    save()
            print("completed native baseline and reconstruction probes", flush=True)
        finally:
            await remote.close({"start": start, "binding": remote.binding})
    finally:
        client.close()
