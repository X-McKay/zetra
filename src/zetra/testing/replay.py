"""Standalone cold reconstruction for offline conformance and recorded tapes."""

import asyncio
import json
import sys

from .._runtime.capture import capture_boundary
from .reporter import factory


async def main():
    request = json.load(sys.stdin)
    result = await capture_boundary(
        lambda bridge: factory(
            bridge, request["workspace"], provider=request.get("provider", "scripted")
        ),
        request["tape"],
        request["pending"],
        invocation=request["session"],
        manifest=request["manifest"],
        message=request["message"],
    )
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
