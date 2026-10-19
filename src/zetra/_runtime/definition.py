"""Import and validate an immutable agent definition inside its sandbox only."""

import importlib
from importlib.resources import files
from pathlib import Path

from ..agent import AgentContext
from ..manifest import AgentSpec, resource_parts
from .records import decode_model, encode_model, thaw
from .vllm import MODEL, VllmExecutor


async def fixture_model(payload, state, key):
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    request = decode_model(LlmRequest, thaw(payload))
    completed = {
        p.function_response.name
        for c in request.contents
        for p in c.parts or []
        if p.function_response
    }
    declarations = [d for t in request.config.tools or [] for d in t.function_declarations or []]
    pending = next((d for d in declarations if d.name not in completed), None)
    if pending:
        schema = pending.parameters_json_schema or {}
        if not schema and pending.parameters:
            schema = pending.parameters.model_dump(exclude_none=True)
        props = schema.get("properties", {})
        args = {}
        for name in schema.get("required", []):
            prop = props[name]
            kind = str(prop.get("type", "string")).lower().split(".")[-1]
            args[name] = (
                "."
                if name == "path"
                else "Workspace inspected."
                if name == "content"
                else 1
                if kind in ("integer", "number")
                else False
                if kind == "boolean"
                else "guide"
            )
        part = types.Part(
            function_call=types.FunctionCall(
                name=pending.name, args=args, id=f"fixture-{len(completed)}"
            )
        )
    else:
        part = types.Part(text="Completed.")
    return encode_model(LlmResponse(content=types.Content(role="model", parts=[part])))


class Definition:
    def __init__(self, spec, workspace):
        self.spec = AgentSpec.model_validate(spec).validate_policy()
        self.workspace = Path(workspace)
        self.instruction = ""
        if self.spec.instructions:
            package, resource = resource_parts(self.spec.instructions)
            self.instruction = (
                files(package).joinpath(resource.as_posix()).read_text(encoding="utf-8")
            )
        module, name = self.spec.entrypoint.split(":")
        self.factory = getattr(importlib.import_module(module), name)
        self.model = MODEL if self.spec.model == "vllm" else "fixture"
        context = self.context(None)
        agent = context.seal(self.factory(context))
        self.inventory = [
            (t.name, t._get_declaration().model_dump(mode="json")) for t in agent.tools
        ]
        self.registry = context.executors()
        self.registry[self.model] = VllmExecutor() if self.spec.model == "vllm" else fixture_model

    def build(self, bridge):
        context = self.context(bridge)
        agent = context.seal(self.factory(context))
        inventory = [(t.name, t._get_declaration().model_dump(mode="json")) for t in agent.tools]
        if inventory != self.inventory:
            raise ValueError("factory registration changed")
        return agent

    def context(self, bridge):
        return AgentContext(
            bridge, workspace=self.workspace, model=self.model, instruction=self.instruction
        )
