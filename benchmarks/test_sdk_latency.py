"""Public SDK wall/phase timings; fresh immutable release sandbox for every sample."""

import json
import os
import time
import uuid
from pathlib import Path

import pytest
from temporalio.client import Client

from benchmarks.test_latency import activity_timings
from zetra.client import SessionClient
from zetra.manifest import load_release


@pytest.mark.cluster
async def test_sdk_latency(tmp_path):
    if os.environ.get("BRIDGE_LATENCY") != "1":
        pytest.skip("opt-in benchmark sends synthetic live inference")
    client = SessionClient(state=tmp_path, address=os.environ["TEMPORAL_ADDRESS"])
    temporal = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    samples = []
    output = Path(".state/sdk-latency.json")
    for release_name in ["reporter"] * 3 + ["live"]:
        release = load_release(f".state/{release_name}-release.json")
        session = "sdk-latency-" + uuid.uuid4().hex
        begin = time.perf_counter()
        result = await client.run(
            release,
            session=session,
            message="Inspect the workspace, write a summary and confirm completion.",
        )
        wall = time.perf_counter() - begin
        assert result["status"] == "SUCCEEDED", result
        assert result["cleanup"] == "DELETED"
        history = await temporal.get_workflow_handle(session).fetch_history()
        rows = activity_timings(history)
        active = [row for row in rows if row["name"] not in ("provision_session", "close_session")]
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
        output.write_text(json.dumps({"samples": samples}, indent=2) + "\n")
        print(
            f"SDK {release.agent.model}: {wall:.3f}s including provision and confirmed deletion",
            flush=True,
        )
