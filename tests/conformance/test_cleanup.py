import asyncio

import pytest

from tests.fixtures.cases import FixtureCase, build_agent
from tests.fixtures.reference import run_native_reference
from zetra._runtime.capture import (
    BridgeModel,
    BridgeTool,
    UnsupportedProfile,
    capture_boundary,
)


async def test_capture_cancels_and_awaits_adk_before_return():
    case = FixtureCase()
    result = await capture_boundary(
        lambda b: build_agent(case, BridgeModel(b), lambda f: BridgeTool(f, b)), []
    )
    assert result["status"] == "operation"
    assert case.effects == []
    assert case.trace == ["before_model", "after_agent"]
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]


async def test_native_final_callback_completes_before_done():
    case = FixtureCase()
    native = await run_native_reference(case)
    tape = [{"operation": o, "outcome": v} for o, v in zip(case.operations, case.outcomes)]
    observed = FixtureCase()
    result = await capture_boundary(
        lambda b: build_agent(observed, BridgeModel(b), lambda f: BridgeTool(f, b)), tape
    )
    assert result["result"] == native
    assert result["result"]["session"]["data"]["state"]["finished"] is True


async def test_two_calls_rejected_before_any_tool():
    from google.genai import types

    from zetra._runtime.records import encode_model

    case = FixtureCase()
    await run_native_reference(case)
    response = case.responses()[0]
    response.content.parts.append(
        types.Part(
            function_call=types.FunctionCall(name="write_report", args={"content": "x"}, id="c2")
        )
    )
    tape = [{"operation": case.operations[0], "outcome": encode_model(response)}]
    observed = FixtureCase()
    with pytest.raises(UnsupportedProfile):
        await capture_boundary(
            lambda b: build_agent(observed, BridgeModel(b), lambda f: BridgeTool(f, b)), tape
        )
    assert observed.effects == []
