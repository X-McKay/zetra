"""ADK boundary capture. It describes operations; it never invokes effect bodies."""

import asyncio
import functools
import inspect
import json

from google.adk.models._capabilities import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.tools import FunctionTool
from pydantic import PrivateAttr

from .adk_compat import run_agent, validate_profile
from .records import canonical_bytes, decode_model, describe, encode_model, encode_value


class ReplayMismatch(ValueError):
    pass


class UnsupportedProfile(ValueError):
    pass


def copy_json(value):
    return json.loads(canonical_bytes(value))


class TapeBridge:
    def __init__(
        self, tape, expected_pending=None, *, invocation="probe-invocation", manifest="probe-v1"
    ):
        self.invocation = invocation
        self.manifest = manifest
        self.tape = copy_json(tape)
        self.expected_pending = copy_json(expected_pending)
        self.cursor = 0
        self.boundary = asyncio.get_running_loop().create_future()

    async def invoke(self, kind, executor, request):
        if self.cursor >= 64:
            raise UnsupportedProfile("boundary budget exhausted")
        operation = describe(
            kind,
            executor,
            request,
            index=self.cursor,
            invocation=self.invocation,
            manifest=self.manifest,
        )
        if self.cursor < len(self.tape):
            entry = self.tape[self.cursor]
            if canonical_bytes(operation) != canonical_bytes(entry["operation"]):
                raise ReplayMismatch(f"changed request at position {self.cursor}")
            self.cursor += 1
            return copy_json(entry["outcome"])
        if self.expected_pending is not None and operation != self.expected_pending:
            raise ReplayMismatch("pending descriptor changed")
        if not self.boundary.done():
            self.boundary.set_result(operation)
        await asyncio.Future()  # only the capture owner cancels this continuation


class BridgeModel(BaseLlm):
    _bridge: object = PrivateAttr()

    def __init__(self, bridge, model="fixture"):
        super().__init__(model=model)
        self._bridge = bridge

    @property
    def capabilities(self):
        return LlmCapabilities()

    async def generate_content_async(self, llm_request, stream=False):
        if stream:
            raise UnsupportedProfile("streaming")
        wire = await self._bridge.invoke("model", self.model, encode_model(llm_request))
        response = decode_model(LlmResponse, wire)
        calls = (
            [p.function_call for p in (response.content.parts or []) if p.function_call]
            if response.content
            else []
        )
        if len(calls) > 1:
            raise UnsupportedProfile("multiple function calls")
        if response.partial or response.interrupted:
            raise UnsupportedProfile("partial model response")
        yield response


class BridgeTool(FunctionTool):
    def __init__(self, function, bridge):
        super().__init__(function)
        self.bridge = bridge

    async def run_async(self, *, args, tool_context):
        return await self.bridge.invoke(
            "tool",
            self.name,
            {"arguments": encode_value(args), "state": encode_value(tool_context.state.to_dict())},
        )


async def capture_boundary(
    factory,
    tape,
    expected_pending=None,
    *,
    invocation="probe-invocation",
    manifest="probe-v1",
    message="Inspect this repository",
):
    bridge = TapeBridge(tape, expected_pending, invocation=invocation, manifest=manifest)

    def admitted_factory():
        agent = factory(bridge)
        validate_profile(agent)
        guard_callbacks(agent)
        return agent

    task = asyncio.create_task(run_agent(admitted_factory, message, invocation=invocation))
    try:
        async with asyncio.timeout(30):
            done, _ = await asyncio.wait(
                [task, bridge.boundary], return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                result = await task
                if bridge.cursor != len(tape) or expected_pending is not None:
                    raise ReplayMismatch("premature finalization")
                return {"status": "done", "result": result}
            return {"status": "operation", "operation": await bridge.boundary}
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        bridge.boundary.cancel()


def guard_callbacks(agent):
    """Reject synthetic results and mutation of pending model/tool requests."""
    names = (
        "before_agent_callback",
        "after_agent_callback",
        "before_model_callback",
        "after_model_callback",
        "before_tool_callback",
        "after_tool_callback",
    )

    def guard(fn, name):
        @functools.wraps(fn)
        async def checked(*args, **kwargs):
            key = {
                "before_model_callback": "llm_request",
                "after_model_callback": "llm_response",
                "before_tool_callback": "args",
            }.get(name)

            def snapshot():
                if not key:
                    return None
                obj = kwargs[key]
                return canonical_bytes(encode_model(obj) if key != "args" else obj)

            before = snapshot()
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            if result is not None or before != snapshot():
                raise UnsupportedProfile("callback override or request mutation")

        return checked

    for name in names:
        callbacks = getattr(agent, name)
        if callbacks is not None:
            setattr(
                agent,
                name,
                [guard(fn, name) for fn in callbacks]
                if isinstance(callbacks, list)
                else guard(callbacks, name),
            )
