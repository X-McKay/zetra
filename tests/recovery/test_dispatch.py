import asyncio
import copy

import pytest

from zetra._runtime.dispatch import Dispatcher, OutcomeUnknown
from zetra._runtime.records import describe


async def test_duplicate_delivery_executes_once_and_uses_frozen_arguments():
    entered = asyncio.Event()
    release = asyncio.Event()
    seen = []

    async def body(arguments, state, operation_id):
        entered.set()
        await release.wait()
        seen.append((arguments["path"], operation_id))
        with pytest.raises(TypeError):
            state["new"] = "forbidden"
        return {"path": arguments["path"]}

    dispatcher = Dispatcher({"inspect": body}, manifest="probe-v1")
    operation = describe("tool", "inspect", {"arguments": {"path": "."}, "state": {}}, index=0)
    a = asyncio.create_task(dispatcher.execute(operation, "attempt-a"))
    await entered.wait()
    b = asyncio.create_task(dispatcher.execute(copy.deepcopy(operation), "attempt-a"))
    operation["request"]["arguments"]["path"] = "changed"
    release.set()
    first, second = await asyncio.gather(a, b)
    assert seen == [(".", "probe-invocation:00000000")]
    assert first == second == {"status": "succeeded", "value": {"path": "."}}
    with pytest.raises(ValueError):
        await dispatcher.execute(operation, "attempt-a")


async def test_failure_after_effect_is_unknown_and_not_retried():
    effects = []

    async def body(arguments, state, operation_id):
        effects.append(operation_id)
        raise OSError("response lost")

    dispatcher = Dispatcher({"unsafe": body}, manifest="probe-v1")
    operation = describe("tool", "unsafe", {"arguments": {}, "state": {}}, index=0)
    for _ in range(2):
        with pytest.raises(OutcomeUnknown):
            await dispatcher.execute(operation, "attempt-a")
    assert effects == ["probe-invocation:00000000"]
