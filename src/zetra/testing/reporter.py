"""Small admitted example: static ADK factory and an immutable executor registry."""

from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from .._runtime.capture import BridgeModel, BridgeTool
from .._runtime.records import decode_model, encode_model


def functions(root):
    root = Path(root)

    async def inspect_repository(path: str) -> dict:
        """List files in the session workspace."""
        if path != ".":
            raise ValueError("only the session root is admitted")
        return {"files": sorted(p.name for p in root.iterdir() if p.is_file())}

    async def write_report(content: str) -> dict:
        """Write a plain text report in the session workspace."""
        (root / "report.md").write_text(content)
        return {"path": "report.md"}

    return [inspect_repository, write_report]


def factory(bridge, root, *, provider="scripted"):
    if provider not in ("scripted", "vllm"):
        raise ValueError("unsupported provider")
    from .._runtime.vllm import MODEL

    return LlmAgent(
        name="reporter",
        instruction=(
            "Inspect the workspace with inspect_repository using path '.'. "
            "Then call write_report once with a brief report listing the filenames. "
            "Finally give a short confirmation. Use exactly these two tools in order, "
            "one tool call per turn. Do not repeat completed tools."
        )
        if provider == "vllm"
        else "Inspect the workspace and write a report.",
        model=BridgeModel(bridge, model=MODEL if provider == "vllm" else "fixture"),
        tools=[BridgeTool(fn, bridge) for fn in functions(root)],
    )


def registry(root, *, provider="scripted"):
    async def model(payload, state, key):
        request = decode_model(LlmRequest, thaw(payload))
        completed = [
            p.function_response.name
            for c in request.contents
            for p in (c.parts or [])
            if p.function_response
        ]
        if "write_report" in completed:
            part = types.Part(text="Report ready.")
        elif "inspect_repository" in completed:
            part = types.Part(
                function_call=types.FunctionCall(
                    name="write_report", args={"content": "Workspace inspected."}, id="write-1"
                )
            )
        else:
            part = types.Part(
                function_call=types.FunctionCall(
                    name="inspect_repository", args={"path": "."}, id="inspect-1"
                )
            )
        return encode_model(LlmResponse(content=types.Content(role="model", parts=[part])))

    if provider == "vllm":
        from .._runtime.vllm import MODEL, VllmExecutor

        result = {MODEL: VllmExecutor()}
    elif provider == "scripted":
        result = {"fixture": model}
    else:
        raise ValueError("unsupported provider")
    for fn in functions(root):

        async def execute(args, state, key, function=fn):
            return await function(**dict(args))

        result[fn.__name__] = execute
    return result


def thaw(value):
    from collections.abc import Mapping

    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value


class Definition:
    def __init__(self, workspace, provider="scripted"):
        self.workspace, self.provider = workspace, provider
        self.registry = registry(workspace, provider=provider)

    def build(self, bridge):
        return factory(bridge, self.workspace, provider=self.provider)
