"""Replay sanitized accepted responses captured from the real local vLLM model."""

import json
from pathlib import Path

import pytest
from google.adk.models.llm_response import LlmResponse

from zetra._runtime.process import run_child
from zetra._runtime.records import decode_model, encode_model


@pytest.mark.parametrize("recording", ["vllm_recording.json", "vllm_warm_recording.json"])
async def test_real_vllm_recording_reconstructs_each_prefix_in_fresh_process(tmp_path, recording):
    recorded = json.loads((Path(__file__).parents[1] / "fixtures" / recording).read_text())
    for entry in recorded["entries"]:
        if entry["operation"]["kind"] == "model":
            assert encode_model(decode_model(LlmResponse, entry["outcome"])) == entry["outcome"]
    for cursor in range(len(recorded["entries"]) + 1):
        result = await run_child(
            "zetra.testing.replay",
            {
                "tape": recorded["entries"][:cursor],
                "pending": None,
                "workspace": str(tmp_path),
                "session": recorded["invocation"],
                "manifest": recorded["manifest"],
                "message": recorded["message"],
                "provider": "vllm",
            },
            timeout=30,
        )
        if cursor < len(recorded["entries"]):
            assert result["operation"] == recorded["entries"][cursor]["operation"]
        else:
            assert result["status"] == "done"
            assert result["result"]["final_text"] == recorded["final_text"]
    # No tool bodies ran during reconstruction, including write_report.
    assert not (tmp_path / "report.md").exists()


@pytest.mark.parametrize("recording", ["vllm_recording.json", "vllm_warm_recording.json"])
async def test_recorded_vllm_warm_continuation_survives_cache_loss(tmp_path, recording):
    from zetra._runtime.warm import WarmRunner
    from zetra.testing.reporter import factory

    recorded = json.loads((Path(__file__).parents[1] / "fixtures" / recording).read_text())
    runner = WarmRunner(
        lambda bridge: factory(bridge, tmp_path, provider="vllm"),
        invocation=recorded["invocation"],
        manifest=recorded["manifest"],
        message=recorded["message"],
    )
    try:
        for cursor in range(6):
            if cursor == 3:
                await runner.close()
            step = await runner.advance(recorded["entries"][:cursor], None)
            if cursor < 5:
                assert step["operation"] == recorded["entries"][cursor]["operation"]
            else:
                assert step["result"]["final_text"] == recorded["final_text"]
        assert not (tmp_path / "report.md").exists()
    finally:
        await runner.close()
