"""Volatile ADK continuation; only an explicitly supplied accepted tape resumes it.

One owner per interpreter: ADK's deterministic providers are process-global.
The session runtime serializes commands and shares its sandbox security domain.
"""

import asyncio
from contextlib import suppress

from .adk_compat import run_agent, validate_profile
from .capture import ReplayMismatch, TapeBridge, copy_json, guard_callbacks


class ContinuingBridge(TapeBridge):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reply = None

    async def invoke(self, kind, executor, request):
        if self.cursor < len(self.tape):
            return await super().invoke(kind, executor, request)
        # Reuse the cold bridge's descriptor, budget and pending checks. It parks
        # after publishing the boundary; cancel that capture only after acceptance.
        capture = asyncio.create_task(super().invoke(kind, executor, request))
        self.reply = asyncio.get_running_loop().create_future()
        try:
            done, _ = await asyncio.wait(
                [capture, self.boundary], return_when=asyncio.FIRST_COMPLETED
            )
            if capture in done:
                return await capture  # propagate descriptor/budget failures
            value = await self.reply
            self.cursor += 1
            return copy_json(value)
        finally:
            capture.cancel()
            with suppress(asyncio.CancelledError):
                await capture


class WarmRunner:
    def __init__(
        self,
        factory,
        *,
        invocation="probe-invocation",
        manifest="probe-v1",
        message="Inspect this repository",
        close_timeout=1.0,
    ):
        self.factory = factory
        self.invocation = invocation
        self.manifest = manifest
        self.message = message
        self.close_timeout = close_timeout
        self.task = None
        self.bridge = None
        self.accepted = None
        self.result = None
        self.poisoned = False
        self.close_lock = asyncio.Lock()

    async def close(self):
        async with self.close_lock:
            was_poisoned = self.poisoned
            # Fence before yielding: caller cancellation must not expose a live
            # finalizer alongside a reusable cached operation. Interrupted or
            # timed-out cleanup permanently poisons this sandbox instance.
            self.poisoned = True
            if self.task is not None:
                self.task.cancel()
                done, _ = await asyncio.wait([self.task], timeout=self.close_timeout)
                if not done:
                    raise RuntimeError("warm task did not quiesce; replace sandbox")
                with suppress(asyncio.CancelledError, Exception):
                    await self.task
            if self.bridge is not None:
                self.bridge.boundary.cancel()
                if self.bridge.reply is not None:
                    self.bridge.reply.cancel()
            self.task = self.bridge = self.accepted = self.result = None
            self.poisoned = was_poisoned

    def start(self, tape, pending):
        self.bridge = ContinuingBridge(
            tape, pending, invocation=self.invocation, manifest=self.manifest
        )

        def factory():
            agent = self.factory(self.bridge)
            validate_profile(agent)
            guard_callbacks(agent)
            return agent

        self.task = asyncio.create_task(
            run_agent(factory, self.message, invocation=self.invocation)
        )
        self.accepted = copy_json(tape)

    async def advance(self, tape, pending):
        if self.poisoned:
            raise RuntimeError("poisoned warm cache; replace sandbox")
        tape = copy_json(tape)
        try:
            if self.result is not None and tape == self.accepted:
                result = self.result
            else:
                resume = (
                    self.result is not None
                    and self.result["status"] == "operation"
                    and len(tape) == len(self.accepted) + 1
                    and tape[:-1] == self.accepted
                    and tape[-1]["operation"] == self.result["operation"]
                    and not self.task.done()
                )
                if resume:
                    self.bridge.boundary = asyncio.get_running_loop().create_future()
                    self.bridge.expected_pending = copy_json(pending)
                    self.accepted = tape
                    self.result = None
                    self.bridge.reply.set_result(copy_json(tape[-1]["outcome"]))
                else:
                    await self.close()
                    self.start(tape, pending)
                done, _ = await asyncio.wait(
                    [self.task, self.bridge.boundary],
                    timeout=30,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    raise TimeoutError("warm advance deadline")
                if self.task in done:
                    value = await self.task
                    if self.bridge.cursor != len(tape) or pending is not None:
                        raise ReplayMismatch("premature finalization")
                    result = {"status": "done", "result": value}
                else:
                    result = {"status": "operation", "operation": await self.bridge.boundary}
                self.result = copy_json(result)
            if pending is not None and (
                result["status"] != "operation" or result["operation"] != pending
            ):
                raise ReplayMismatch("pending descriptor changed")
            return copy_json(result)
        except BaseException:
            await self.close()
            raise
