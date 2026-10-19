"""Trusted scripted agent. Native execution is independent of bridge replay."""

import copy
import hashlib
from dataclasses import dataclass, field

from google.adk.agents import LlmAgent
from google.adk.models._capabilities import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr

from zetra._runtime.records import describe, encode_model


@dataclass
class FixtureCase:
    scenario: str = "normal"
    operations: list = field(default_factory=list)
    outcomes: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    effects: list = field(default_factory=list)

    def responses(self):
        return [
            LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                name="inspect_repository", args={"path": "."}, id="c1"
                            )
                        )
                    ],
                )
            ),
            LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                name="write_report", args={"content": "README.md"}, id="c2"
                            )
                        )
                    ],
                )
            ),
            LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text="Report ready.")])
            ),
        ]

    def record(self, kind, name, request, result):
        self.operations.append(describe(kind, name, request, index=len(self.operations)))
        self.outcomes.append(copy.deepcopy(result))
        self.effects.append(name)


class ScriptedModel(BaseLlm):
    _case: FixtureCase = PrivateAttr()
    _cursor: int = PrivateAttr(default=0)

    def __init__(self, case):
        super().__init__(model="fixture")
        self._case = case

    @property
    def capabilities(self):
        return LlmCapabilities()

    async def generate_content_async(self, llm_request, stream=False):
        result = self._case.responses()[self._cursor]
        self._cursor += 1
        self._case.record("model", "fixture", encode_model(llm_request), encode_model(result))
        yield result


def build_agent(case, model=None, wrap=None):
    async def inspect_repository(path: str) -> dict:
        """List the fixture repository at path."""
        result = {"error": "MISSING_FILE"} if case.scenario == "error" else {"files": ["README.md"]}
        case.record(
            "tool", "inspect_repository", {"arguments": {"path": path}, "state": {}}, result
        )
        return result

    async def write_report(content: str) -> dict:
        """Write a report from content."""
        result = {"path": "report.md", "sha256": hashlib.sha256(content.encode()).hexdigest()}
        case.record(
            "tool",
            "write_report",
            {"arguments": {"content": content}, "state": {"inspected": True}},
            result,
        )
        return result

    def before_model(callback_context, llm_request):
        case.trace.append("before_model")

    def after_model(callback_context, llm_response):
        case.trace.append("after_model")

    def before_tool(tool, args, tool_context):
        case.trace.append("before:" + tool.name)

    def after_tool(tool, args, tool_context, tool_response):
        case.trace.append("after:" + tool.name)
        if tool.name == "inspect_repository":
            tool_context.state["inspected"] = True
        else:
            tool_context.state["report_path"] = tool_response["path"]
        if case.scenario == "mutate":
            tool_response["callback_marker"] = "observed"

    def after_agent(callback_context):
        case.trace.append("after_agent")
        callback_context.state["finished"] = True

    tools = [inspect_repository, write_report]
    if wrap:
        tools = [wrap(t) for t in tools]
    return LlmAgent(
        name="reporter",
        model=model or ScriptedModel(case),
        instruction="Inspect the repository then write a report.",
        tools=tools,
        before_model_callback=before_model,
        after_model_callback=after_model,
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_agent_callback=after_agent,
    )
