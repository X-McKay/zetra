"""Full supervisor conformance at the maximum accepted effect count."""

import pytest
from google.adk.agents import LlmAgent
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from zetra._runtime.artifacts import Receipt
from zetra._runtime.capture import BridgeModel, BridgeTool, UnsupportedProfile
from zetra._runtime.records import encode_model
from zetra._runtime.runtime import Runtime


class RepeatingDefinition:
    def __init__(self):
        self.calls = []

        async def model(payload, state, key):
            self.calls.append(("model", key))
            return encode_model(
                LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                function_call=types.FunctionCall(name="tick", args={}, id="tick")
                            )
                        ],
                    )
                )
            )

        async def tick(arguments, state, key):
            self.calls.append(("tool", key))
            return {"ok": True}

        self.registry = {"fixture": model, "tick": tick}

    def build(self, bridge):
        async def tick() -> dict:
            """Record one tick."""
            raise AssertionError("effect body must be dispatched")

        return LlmAgent(
            name="repeat",
            instruction="Call tick repeatedly.",
            model=BridgeModel(bridge),
            tools=[BridgeTool(tick, bridge)],
        )


async def test_64_effects_survive_cold_recovery_and_reject_boundary_65(tmp_path):
    definition = RepeatingDefinition()

    def fresh_runtime():
        return Runtime(
            tmp_path,
            definition=definition,
            session="limit",
            manifest="limit-v1",
            key=b"x" * 32,
            message="Repeat",
        )

    runtime = fresh_runtime()
    position = runtime.tape.empty(runtime.workspace.capture())

    async def call(action, **extra):
        return await runtime.handle(
            runtime.authority.sign(
                action,
                {
                    "binding": {"session": "limit"},
                    "position": position.model_dump(),
                    **extra,
                },
            )
        )

    try:
        for index in range(64):
            pending = await call("advance", pending=None)
            assert pending["operation"]["kind"] == ("model" if index % 2 == 0 else "tool")
            if index == 32:
                await runtime.close()
                runtime = fresh_runtime()
                assert await call("advance", pending=pending["operation"]) == pending
                assert len(definition.calls) == 32
            receipt = await call(
                "execute", operation=pending["operation"], attempt_id=f"attempt-{index}"
            )
            position = Receipt.model_validate(receipt["receipt"]).position
        assert position.cursor == 64
        assert len(runtime.tape.load(position)) == len(definition.calls) == 64
        assert [kind for kind, _ in definition.calls] == ["model", "tool"] * 32
        for cold in (False, True):
            if cold:
                await runtime.close()
                runtime = fresh_runtime()
            with pytest.raises(UnsupportedProfile, match="boundary budget exhausted"):
                await call("advance", pending=None)
            assert len(definition.calls) == 64
    finally:
        await runtime.close()
