from zetra._runtime.adk_compat import run_agent

from .cases import build_agent


async def run_native_reference(case):
    return await run_agent(lambda: build_agent(case))
