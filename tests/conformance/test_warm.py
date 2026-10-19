import asyncio
import copy

import pytest

from tests.fixtures.cases import FixtureCase, build_agent
from tests.fixtures.reference import run_native_reference
from zetra._runtime.capture import BridgeModel, BridgeTool, ReplayMismatch


@pytest.mark.parametrize("scenario", ["normal", "error", "mutate"])
async def test_warm_continuation_matches_native_without_repeating_callbacks_or_effects(scenario):
    from zetra._runtime.warm import WarmRunner

    native = FixtureCase(scenario=scenario)
    expected = await run_native_reference(native)
    tape = [
        {"operation": op, "outcome": value} for op, value in zip(native.operations, native.outcomes)
    ]
    observed = FixtureCase(scenario=scenario)
    builds = []

    def factory(bridge):
        builds.append(True)
        return build_agent(
            observed, model=BridgeModel(bridge), wrap=lambda fn: BridgeTool(fn, bridge)
        )

    runner = WarmRunner(factory)
    try:
        for cursor in range(6):
            result = await runner.advance(tape[:cursor], None)
            trace = observed.trace.copy()
            # A retried advance cannot consume an unaccepted result or repeat callbacks.
            assert await runner.advance(tape[:cursor], None) == result
            assert observed.trace == trace
            if cursor < 5:
                assert result["operation"] == tape[cursor]["operation"]
                await asyncio.sleep(0)
                assert observed.trace == trace
            else:
                assert result["result"] == expected
        assert builds == [True]
        assert observed.trace == native.trace
        assert observed.effects == []
    finally:
        await runner.close()


async def test_warm_rejects_wrong_pending_and_cold_reconstructs_changed_prefix():
    from zetra._runtime.warm import WarmRunner

    native = FixtureCase()
    await run_native_reference(native)
    tape = [
        {"operation": op, "outcome": value} for op, value in zip(native.operations, native.outcomes)
    ]
    case = FixtureCase()
    runner = WarmRunner(
        lambda b: build_agent(case, model=BridgeModel(b), wrap=lambda fn: BridgeTool(fn, b))
    )
    try:
        first = await runner.advance([], None)
        with pytest.raises(ReplayMismatch):
            await runner.advance([], {"wrong": "descriptor"})
        # Recovery from a cache discontinuity uses the supplied prefix, not cached state.
        assert (await runner.advance(tape[:3], None))["operation"] == tape[3]["operation"]
        corrupted = copy.deepcopy(tape[:1])
        corrupted[0]["operation"]["executor"] = "wrong-model"
        with pytest.raises(ReplayMismatch):
            await runner.advance(corrupted, None)
        assert (await runner.advance([], None)) == first
        assert case.effects == []
    finally:
        await runner.close()


async def test_failed_warm_cancellation_poisoned_until_sandbox_is_replaced():
    from zetra._runtime.warm import WarmRunner

    entered, release = asyncio.Event(), asyncio.Event()
    case = FixtureCase()

    async def hang(callback_context):
        entered.set()
        await release.wait()

    def factory(b):
        agent = build_agent(case, model=BridgeModel(b), wrap=lambda fn: BridgeTool(fn, b))
        agent.after_agent_callback = hang
        return agent

    runner = WarmRunner(factory, close_timeout=0.05)
    try:
        await runner.advance([], None)
        with pytest.raises(RuntimeError, match="quiesce"):
            await runner.close()
        assert entered.is_set()
        with pytest.raises(RuntimeError, match="poison"):
            await runner.advance([], None)
    finally:
        release.set()
        await asyncio.sleep(0.01)
        await runner.close()


async def test_interrupted_close_fences_live_finalizer_and_cached_operation():
    from zetra._runtime.warm import WarmRunner

    entered, release = asyncio.Event(), asyncio.Event()
    case = FixtureCase()

    async def hang(callback_context):
        entered.set()
        await release.wait()

    def factory(b):
        agent = build_agent(case, model=BridgeModel(b), wrap=lambda fn: BridgeTool(fn, b))
        agent.after_agent_callback = hang
        return agent

    runner = WarmRunner(factory)
    try:
        await runner.advance([], None)
        old_task = runner.task
        closing = asyncio.create_task(runner.close())
        await asyncio.wait_for(entered.wait(), timeout=1)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not old_task.done()
        with pytest.raises(RuntimeError, match="poison"):
            await runner.advance([], None)
        release.set()
        await asyncio.sleep(0.01)
        await runner.close()
        # Late quiescence must not rehabilitate a sandbox with interrupted cleanup.
        with pytest.raises(RuntimeError, match="poison"):
            await runner.advance([], None)
    finally:
        release.set()
        await asyncio.sleep(0.01)
        await runner.close()
