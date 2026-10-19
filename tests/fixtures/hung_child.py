"""A real ADK finalizer that hangs after a boundary is captured."""

import asyncio
import json
import os
import sys
from pathlib import Path

from tests.fixtures.cases import FixtureCase, build_agent
from zetra._runtime.capture import BridgeModel, BridgeTool, capture_boundary

request = json.load(sys.stdin)
if request.get("flood"):
    print("x" * 10000, flush=True)
else:

    async def hang(callback_context):
        Path(request["pid_file"]).write_text(str(os.getpid()))
        await asyncio.Event().wait()

    def factory(bridge):
        case = FixtureCase()
        agent = build_agent(case, BridgeModel(bridge), lambda f: BridgeTool(f, bridge))
        agent.after_agent_callback = hang
        return agent

    asyncio.run(capture_boundary(factory, []))
