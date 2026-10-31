"""One trusted local agent execution in a fresh interpreter; not a sandbox."""

import asyncio
import json
import sys
from contextlib import redirect_stdout

from zetra.local import _run_in_process

if __name__ == "__main__":
    request = json.load(sys.stdin)
    with redirect_stdout(sys.stderr):
        result = asyncio.run(_run_in_process(**request))
    print(json.dumps(result, allow_nan=False))
