from pathlib import Path

from zetra._runtime.process import run_child


async def fresh_advance(request):
    result = await run_child(
        "tests.fixtures.replay_child", request, cwd=Path(__file__).resolve().parents[2], timeout=10
    )
    result["process_exited"] = True  # run_child returns only after wait()/cleanup
    return result
