"""Explicit deterministic qualification entrypoint."""

import asyncio

from .._runtime.runtime import main
from .reporter import Definition

if __name__ == "__main__":
    asyncio.run(main(Definition))
