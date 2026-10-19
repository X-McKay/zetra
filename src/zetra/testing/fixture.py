"""Fresh trusted host process for one fixture evaluation; this is not a sandbox."""

import asyncio
import json
import sys
from contextlib import redirect_stdout

from zetra.evaluation import _fixture_run_in_process

if __name__ == "__main__":
    request = json.load(sys.stdin)
    with redirect_stdout(sys.stderr):
        result = asyncio.run(_fixture_run_in_process(request["root"], request["message"]))
    print(json.dumps(result))
