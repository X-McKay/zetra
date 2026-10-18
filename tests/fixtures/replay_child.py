import asyncio
import json
import sys

from tests.fixtures.cases import FixtureCase, build_agent
from zetra._runtime.capture import BridgeModel, BridgeTool, capture_boundary


async def main():
    request = json.load(sys.stdin)
    case = FixtureCase(scenario=request.get("scenario", "normal"))

    def factory(bridge):
        return build_agent(case, BridgeModel(bridge), lambda fn: BridgeTool(fn, bridge))

    try:
        result = await capture_boundary(
            factory, request.get("tape", []), request.get("expected_pending")
        )
    except ValueError as exc:
        result = {"error": type(exc).__name__, "message": str(exc)}
    result.update(effects=case.effects, trace=case.trace)
    print(json.dumps(result))


asyncio.run(main())
