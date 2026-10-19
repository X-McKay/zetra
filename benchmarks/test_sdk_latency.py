"""SDK timings with a shared ordinary Worker; worker startup is excluded."""

import json
import os
import secrets
import time
import uuid
from pathlib import Path

import pytest
from openshell import SandboxClient, TlsConfig
from temporalio.client import Client

from benchmarks.test_latency import activity_timings
from zetra._runtime.artifacts import FileStore
from zetra.client import SessionClient
from zetra.manifest import load_release
from zetra.worker import create_worker


@pytest.mark.cluster
async def test_sdk_latency(tmp_path):
    if os.environ.get("BRIDGE_LATENCY") != "1":
        pytest.skip("opt-in benchmark sends synthetic live inference")
    temporal = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    tls = Path(os.environ["OPENSHELL_TLS_DIR"])
    gateway = SandboxClient(
        os.environ["OPENSHELL_ADDRESS"],
        tls=TlsConfig(ca_path=tls / "ca.crt", cert_path=tls / "tls.crt", key_path=tls / "tls.key"),
    )
    options = {
        "task_queue": "sdk-latency-" + uuid.uuid4().hex,
        "runtime_id": "sdk-latency-runtime-" + uuid.uuid4().hex,
        "artifacts": FileStore(tmp_path / "objects"),
    }
    client = SessionClient(temporal, **options)
    samples = []
    output = Path(".state/sdk-latency.json")
    try:
        async with create_worker(
            temporal, sandbox_client=gateway, key=secrets.token_bytes(32), **options
        ):
            for release_name in ["reporter"] * 3 + ["live"]:
                release = load_release(f".state/{release_name}-release.json")
                session = "sdk-latency-" + uuid.uuid4().hex
                begin = time.perf_counter()
                handle = await client.submit(
                    release,
                    session=session,
                    message="Inspect the workspace, write a summary and confirm completion.",
                )
                result = await client.wait(handle)
                wall = time.perf_counter() - begin
                assert result["status"] == "SUCCEEDED", result
                assert result["cleanup"] == "DELETED"
                history = await temporal.get_workflow_handle(
                    handle.workflow_id, run_id=handle.run_id
                ).fetch_history()
                rows = activity_timings(history)
                active = [
                    row
                    for row in rows
                    if row["name"]
                    not in (
                        "provision_session",
                        "create_session",
                        "wait_session_ready",
                        "close_session",
                    )
                ]
                samples.append(
                    {
                        "release": release.identity,
                        "model": release.agent.model,
                        "wall_seconds": wall,
                        "active_seconds": active[-1]["completed"] - active[0]["scheduled"],
                        "activities": rows,
                        "cleanup": result["cleanup"],
                    }
                )
                output.write_text(
                    json.dumps({"worker_startup_included": False, "samples": samples}, indent=2)
                    + "\n"
                )
                print(
                    f"SDK {release.agent.model}: {wall:.3f}s including provision and confirmed "
                    "deletion; shared Worker startup excluded",
                    flush=True,
                )
    finally:
        gateway.close()
