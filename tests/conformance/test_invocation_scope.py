import pytest

from zetra._runtime.capture import TapeBridge, UnsupportedProfile


async def test_boundary_ids_are_bound_to_the_invocation():
    import asyncio

    bridge = TapeBridge([], invocation="session-b", manifest="manifest-b")
    task = asyncio.create_task(bridge.invoke("tool", "inspect", {}))
    operation = await bridge.boundary
    assert operation["operation_id"] == "session-b:00000000"
    assert operation["manifest_digest"] == "manifest-b"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_boundary_limit_is_enforced_before_capture():
    bridge = TapeBridge([])
    bridge.cursor = 64
    import asyncio

    with pytest.raises(UnsupportedProfile):
        await asyncio.wait_for(bridge.invoke("model", "fixture", {}), 0.1)
