"""Benchmark-only program sent through OpenShell exec; never part of the agent image."""

import time

started = time.perf_counter()
import asyncio
import json
import resource
import sys
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.models._capabilities import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from zetra._runtime.adk_compat import run_agent
from zetra._runtime.capture import BridgeModel, BridgeTool, UnsupportedProfile, capture_boundary
from zetra._runtime.records import canonical_bytes, decode_model, encode_model
from zetra._runtime.vllm import MODEL
from zetra.testing.reporter import factory, functions, registry

imports_seconds = time.perf_counter() - started
TAPE = Path("/tmp/latency-tape.json")


async def main():
    mode = sys.argv[1]
    if mode == "native":
        provider = sys.argv[2]
        root = Path("/tmp/native-latency")
        root.mkdir(exist_ok=True)
        (root / "report.md").unlink(missing_ok=True)
        (root / "README.md").write_text("Synthetic repository for latency measurement.")
        model = MODEL if provider == "vllm" else "fixture"
        execute = registry(root, provider=provider)[model]
        model_seconds = []

        class NativeModel(BaseLlm):
            @property
            def capabilities(self):
                return LlmCapabilities()

            async def generate_content_async(self, llm_request, stream=False):
                start = time.perf_counter()
                response = await execute(encode_model(llm_request), {}, "native")
                model_seconds.append(time.perf_counter() - start)
                yield decode_model(LlmResponse, response)

        def build():
            return LlmAgent(
                name="reporter",
                model=NativeModel(model=model),
                instruction=factory(None, root, provider=provider).instruction,
                tools=functions(root),
            )

        start = time.perf_counter()
        result = await run_agent(build, "Inspect this repository and write report.md.")
        elapsed = time.perf_counter() - start
        assert result["final_text"] and (root / "report.md").exists()
        assert len(model_seconds) == 3
        return {"body_seconds": elapsed, "model_seconds": model_seconds}

    async def inspect_repository(path: str) -> dict:
        """List files in the session workspace."""
        raise AssertionError("reconstruction executed a tool")

    def build(bridge):
        return LlmAgent(
            name="loop_probe",
            instruction="Inspect repeatedly.",
            model=BridgeModel(bridge),
            tools=[BridgeTool(inspect_repository, bridge)],
        )

    if mode == "prepare":
        tape = []
        for _ in range(64):
            step = await capture_boundary(build, tape)
            op = step["operation"]
            if op["kind"] == "model":
                outcome = encode_model(
                    LlmResponse(
                        content=types.Content(
                            role="model",
                            parts=[
                                types.Part(
                                    function_call=types.FunctionCall(
                                        id=f"call-{op['index']}",
                                        name="inspect_repository",
                                        args={"path": "."},
                                    )
                                )
                            ],
                        )
                    )
                )
            else:
                outcome = {"files": ["README.md"]}
            tape.append({"operation": op, "outcome": outcome})
        TAPE.write_bytes(canonical_bytes(tape))
        return {"tape_bytes": TAPE.stat().st_size}

    cursor = int(sys.argv[2])
    # Parse only the selected prefix: larger unrelated tape bytes are not charged.
    full = json.loads(TAPE.read_bytes())
    prefix = canonical_bytes(full[:cursor])
    start = time.perf_counter()
    cpu = resource.getrusage(resource.RUSAGE_SELF)
    before_cpu = cpu.ru_utime + cpu.ru_stime
    rejected = False
    try:
        step = await capture_boundary(build, json.loads(prefix))
        assert step["operation"]["index"] == cursor
    except UnsupportedProfile:
        assert cursor == 64
        rejected = True
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "cursor": cursor,
        "prefix_bytes": len(prefix),
        "body_seconds": time.perf_counter() - start,
        "cpu_seconds": usage.ru_utime + usage.ru_stime - before_cpu,
        "budget_rejected": rejected,
    }


result = asyncio.run(main())
print(
    json.dumps(
        {
            **result,
            "imports_seconds": imports_seconds,
            "process_seconds": time.perf_counter() - started,
        }
    )
)
