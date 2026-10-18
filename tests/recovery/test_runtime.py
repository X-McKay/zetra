from tests.fixtures.sdk_runtime import Runtime


async def test_real_demo_runs_through_five_warm_boundaries(tmp_path):
    runtime = Runtime(
        tmp_path, session="demo", manifest="demo-v1", key=b"x" * 32, message="Inspect"
    )
    position = runtime.tape.empty(runtime.workspace.capture())
    binding = {"session": "demo"}
    try:
        for cursor in range(6):
            response = await runtime.handle(
                runtime.authority.sign(
                    "advance",
                    {"binding": binding, "position": position.model_dump(), "pending": None},
                )
            )
            if cursor == 5:
                assert (
                    response["status"] == "done"
                    and response["result"]["final_text"] == "Report ready."
                )
                break
            result = await runtime.handle(
                runtime.authority.sign(
                    "execute",
                    {
                        "binding": binding,
                        "position": position.model_dump(),
                        "operation": response["operation"],
                        "attempt_id": f"attempt-{cursor}",
                    },
                )
            )
            from zetra._runtime.artifacts import Receipt

            position = Receipt.model_validate(result["receipt"]).position
        assert len(runtime.effects) == 5
        assert (tmp_path / "workspace" / "report.md").read_text() == "Workspace inspected."
    finally:
        await runtime.close()


async def test_candidate_cannot_resume_warm_agent_until_explicit_accepted_advance(tmp_path):
    runtime = Runtime(
        tmp_path, session="warm", manifest="demo-v1", key=b"x" * 32, message="Inspect"
    )
    position = runtime.tape.empty(runtime.workspace.capture())
    binding = {"session": "warm"}

    async def call(action, position, **extra):
        return await runtime.handle(
            runtime.authority.sign(
                action,
                {
                    "binding": binding,
                    "position": position.model_dump(),
                    **extra,
                },
            )
        )

    try:
        first = await call("advance", position, pending=None)
        execution = await call("execute", position, operation=first["operation"], attempt_id="one")
        # Candidate bytes alone must leave the ADK task parked at its first model.
        assert runtime.warm.bridge.cursor == 0
        assert await call("advance", position, pending=None) == first
        assert runtime.warm.bridge.cursor == 0
        assert len(runtime.effects) == 1
        from zetra._runtime.artifacts import Receipt

        accepted = Receipt.model_validate(execution["receipt"]).position
        second = await call("advance", accepted, pending=None)
        assert second["operation"]["executor"] == "inspect_repository"
        assert runtime.warm.bridge.cursor == 1
        # Lose only the volatile continuation, then reconstruct the same boundary.
        await runtime.warm.close()
        assert await call("advance", accepted, pending=second["operation"]) == second
        assert len(runtime.effects) == 1
    finally:
        await runtime.close()


async def test_poisoned_runtime_cannot_execute_or_restore_workspace(tmp_path):
    import asyncio

    import pytest

    runtime = Runtime(
        tmp_path, session="poison", manifest="demo-v1", key=b"x" * 32, message="Inspect"
    )
    release = asyncio.Event()
    original = runtime.warm.factory

    async def hang(callback_context):
        await release.wait()

    def factory(bridge):
        agent = original(bridge)
        agent.after_agent_callback = hang
        return agent

    runtime.warm.factory = factory
    runtime.warm.close_timeout = 0.05
    position = runtime.tape.empty(runtime.workspace.capture())
    command = {"binding": {"session": "poison"}, "position": position.model_dump(), "pending": None}
    try:
        first = await runtime.handle(runtime.authority.sign("advance", command))
        with pytest.raises(RuntimeError, match="quiesce"):
            await runtime.warm.close()
        marker = runtime.workdir / "not-restored"
        marker.write_text("keep")
        with pytest.raises(RuntimeError, match="poison"):
            await runtime.handle(runtime.authority.sign("advance", command))
        assert marker.read_text() == "keep"
        with pytest.raises(RuntimeError, match="poison"):
            await runtime.handle(
                runtime.authority.sign(
                    "execute",
                    {
                        **command,
                        "operation": first["operation"],
                        "attempt_id": "one",
                    },
                )
            )
        assert runtime.effects == []
    finally:
        release.set()
        await asyncio.sleep(0.01)
        await runtime.close()


async def test_runtime_shutdown_fences_commands_even_when_close_is_cancelled(tmp_path):
    import asyncio

    import pytest

    runtime = Runtime(
        tmp_path, session="close", manifest="demo-v1", key=b"x" * 32, message="Inspect"
    )
    position = runtime.tape.empty(runtime.workspace.capture())
    command = {"binding": {"session": "close"}, "position": position.model_dump(), "pending": None}
    first = await runtime.handle(runtime.authority.sign("advance", command))
    try:
        # An already accepted handler can own the lock when shutdown begins.
        async with runtime.lock:
            closing = asyncio.create_task(runtime.close())
            await asyncio.sleep(0)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
        marker = runtime.workdir / "not-restored"
        marker.write_text("keep")
        for action, extra in [
            ("advance", {}),
            ("execute", {"operation": first["operation"], "attempt_id": "one"}),
        ]:
            with pytest.raises(RuntimeError, match="closing"):
                await runtime.handle(runtime.authority.sign(action, {**command, **extra}))
        assert marker.read_text() == "keep"
        assert runtime.effects == []
        await runtime.close()
        with pytest.raises(RuntimeError, match="closing"):
            await runtime.handle(runtime.authority.sign("advance", command))
    finally:
        await runtime.close()


async def test_runtime_shutdown_cancels_active_executor_before_waiting_for_command_lock(tmp_path):
    import asyncio

    import pytest

    runtime = Runtime(
        tmp_path, session="active-close", manifest="demo-v1", key=b"x" * 32, message="Inspect"
    )
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def hang(arguments, state, key):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    position = runtime.tape.empty(runtime.workspace.capture())
    command = {
        "binding": {"session": "active-close"},
        "position": position.model_dump(),
        "pending": None,
    }
    first = await runtime.handle(runtime.authority.sign("advance", command))
    runtime.dispatcher.registry = {first["operation"]["executor"]: hang}
    execution = asyncio.create_task(
        runtime.handle(
            runtime.authority.sign(
                "execute", {**command, "operation": first["operation"], "attempt_id": "one"}
            )
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.wait_for(runtime.close(), timeout=1)
        assert cancelled.is_set()
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert runtime.candidate is None
        assert runtime.warm.task is None
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        await runtime.close()
