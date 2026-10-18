"""Opt-in live inference through the real OpenShell + Temporal kind deployment."""

import asyncio
import json
import os
import secrets
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from google.adk.models.llm_response import LlmResponse
from openshell import SandboxClient, TlsConfig
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from tests.fixtures.sandbox import OpenShellSession
from zetra._runtime.activities import Activities
from zetra._runtime.artifacts import FileStore, Position, Tape
from zetra._runtime.openshell import PYTHON
from zetra._runtime.records import decode_model, encode_model
from zetra._runtime.temporal import SessionWorkflow
from zetra._runtime.vllm import MANIFEST, MODEL
from zetra._runtime.workspace import Workspace

pytestmark = [pytest.mark.cluster, pytest.mark.live]


class ObservedTransport:
    def __init__(self, remote, *, lose_completion=False):
        self.remote = remote
        self.operations = []
        self.lose_completion = lose_completion

    async def call(self, action, command):
        result = await self.remote.call(action, command)
        if action == "execute":
            self.operations.append(command["operation"])
            if self.lose_completion:
                self.lose_completion = False
                raise ConnectionError(
                    "injected loss after real provider result and candidate upload"
                )
        return result


@pytest.mark.parametrize("scenario", ["normal", "replacement", "completion_loss"])
async def test_live_vllm_in_sandbox(tmp_path, scenario):
    if os.environ.get("BRIDGE_LIVE_VLLM") != "1":
        pytest.skip("set BRIDGE_LIVE_VLLM=1 to authorize synthetic live inference")
    for name in ("OPENSHELL_TLS_DIR", "TEMPORAL_ADDRESS"):
        if not os.environ.get(name):
            pytest.fail(f"live gate requires {name}; run scripts/kind_test.py")
    session = "live-" + uuid.uuid4().hex
    store = FileStore(tmp_path / "objects")
    base = tmp_path / "base"
    base.mkdir()
    (base / "README.md").write_text("Synthetic repository for live qualification.")
    tape = Tape(store, manifest=MANIFEST)
    position = tape.empty(Workspace(base, store).capture())
    tls = Path(os.environ["OPENSHELL_TLS_DIR"])
    client = SandboxClient(
        os.environ["OPENSHELL_ADDRESS"],
        tls=TlsConfig(ca_path=tls / "ca.crt", cert_path=tls / "tls.crt", key_path=tls / "tls.key"),
    )
    remote = OpenShellSession(
        client,
        store,
        session=session,
        manifest=MANIFEST,
        key=secrets.token_bytes(32),
        provider="vllm",
    )
    transport = ObservedTransport(remote, lose_completion=scenario == "completion_loss")
    activities = Activities(remote, transport, tape)
    temporal = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    # Pause at the first completed tool. Its files and model response must survive replacement.
    start = {
        "session": session,
        "message": "Inspect this repository and write report.md.",
        "position": position.model_dump(),
        "pause_at": 2,
    }
    try:
        async with Worker(
            temporal,
            task_queue=session,
            workflows=[SessionWorkflow],
            activities=activities.registered(),
        ):
            handle = await temporal.start_workflow(
                SessionWorkflow.run,
                start,
                id=session,
                task_queue=session,
                execution_timeout=timedelta(minutes=5),
            )
            if scenario != "completion_loss":
                async with asyncio.timeout(150):
                    while True:
                        progress = await handle.query(SessionWorkflow.progress)
                        if progress["position"] and progress["position"]["cursor"] == 2:
                            break
                        description = await handle.describe()
                        if description.close_time:
                            pytest.fail(
                                f"live workflow ended before pause: {await handle.result()}"
                            )
                        await asyncio.sleep(0.5)
                # The allowed endpoint must not become a broad host/path or internet grant.
                probe = await asyncio.to_thread(
                    client.exec,
                    remote.binding["name"],
                    [
                        PYTHON,
                        "-c",
                        """import httpx,json
out={}
for key,url,method in [('models','https://llm.almckay.io/v1/models','GET'),('other_host','https://example.com/','GET'),('wrong_method','https://llm.almckay.io/v1/chat/completions','GET')]:
 try:
  r=httpx.request(method,url,timeout=5);out[key]={"status":r.status_code}
 except httpx.RequestError as exc:
  out[key]={"error":type(exc).__name__}
print(json.dumps(out))""",
                    ],
                    workspace="default",
                    timeout_seconds=20,
                    no_login_shell=True,
                )
                assert probe.exit_code == 0, probe.stderr
                network = json.loads(probe.stdout)
                assert network["models"] == {"status": 403}, network
                assert network["wrong_method"] == {"status": 403}, network
                assert "error" in network["other_host"], network
                # Host denial occurs before HTTP in transparent mode. A connection
                # failure alone is insufficient: require the trusted supervisor's
                # exact policy decision for this immutable sandbox instance.
                logs = await asyncio.to_thread(
                    subprocess.check_output,
                    [
                        "kubectl",
                        "--kubeconfig",
                        os.environ["BRIDGE_KUBECONFIG"],
                        "-n",
                        "openshell",
                        "logs",
                        "os-supervisor-" + remote.binding["sandbox_id"],
                        "--all-containers=true",
                    ],
                    text=True,
                    timeout=15,
                )
                denied = [
                    line
                    for line in logs.splitlines()
                    if "DENIED" in line
                    and "-> example.com:443" in line
                    and "[reason:transparent_tcp_policy_denied]" in line
                ]
                assert denied, {"network": network, "supervisor_denial": denied}
                network["other_host"]["supervisor_denial"] = denied
                if scenario == "replacement":
                    old = remote.binding.copy()
                    assert (await remote.close({"binding": old}))["cleanup"] == "DELETED"
                await handle.signal(SessionWorkflow.resume)
            result = await handle.result()
        assert result["cleanup"] == "DELETED", result
        history = await handle.fetch_history()
        await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)
        if scenario == "completion_loss":
            assert result["status"] == "OUTCOME_UNKNOWN"
            assert result["position"]["cursor"] == 0
            assert len(transport.operations) == 1
            assert transport.operations[0]["executor"] == MODEL
            return
        assert result["status"] == "SUCCEEDED", result
        entries = tape.load(Position.model_validate(result["position"]))
        tools = [e["operation"]["executor"] for e in entries if e["operation"]["kind"] == "tool"]
        assert tools == ["inspect_repository", "write_report"]
        assert len(transport.operations) == len(entries) == 5
        model_entries = [e for e in entries if e["operation"]["kind"] == "model"]
        assert len(model_entries) == 3
        tokens = 0
        for entry in model_entries:
            assert entry["operation"]["executor"] == MODEL
            value = entry["outcome"]
            response = decode_model(LlmResponse, value)
            assert encode_model(response) == value
            assert response.usage_metadata.total_token_count > 0
            tokens += response.usage_metadata.total_token_count
        restored = tmp_path / "restored"
        restored.mkdir()
        Workspace(restored, store).restore(Position.model_validate(result["position"]).workspace)
        report = (restored / "report.md").read_text()
        assert "README.md" in report and len(report) < 10000
        assert result["result"]["final_text"]
        if scenario == "replacement":
            assert remote.binding["generation"] == 2
            assert remote.binding["sandbox_id"] != old["sandbox_id"]
        # Synthetic accepted bytes are suitable for offline conformance replay.
        evidence = {
            "scenario": scenario,
            "model": MODEL,
            "tokens": tokens,
            "network": network,
            "position": result["position"],
            "entries": entries,
            "report": report,
            "final_text": result["result"]["final_text"],
            "workflow_id": session,
            "cleanup": result["cleanup"],
        }
        output = Path(os.environ.get("BRIDGE_LIVE_EVIDENCE_DIR", tmp_path / "evidence"))
        output.mkdir(parents=True, exist_ok=True)
        (output / f"{scenario}.json").write_text(json.dumps(evidence, indent=2))
    finally:
        assert (await remote.close({"start": start, "binding": remote.binding}))[
            "cleanup"
        ] == "DELETED"
        client.close()
