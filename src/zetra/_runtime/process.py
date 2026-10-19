"""Bounded subprocess lifecycle. Module selection is trusted deployment config."""

import asyncio
import json
import os
import signal
import sys

from .records import canonical_bytes


async def run_child(module, request, *, timeout=30, cwd=None, max_output=1048576):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        module,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        cwd=cwd,
    )

    async def bounded_read(stream):
        chunks = []
        size = 0
        while chunk := await stream.read(65536):
            size += len(chunk)
            if size > max_output:
                raise ValueError("child output exceeds limit")
            chunks.append(chunk)
        return b"".join(chunks)

    tasks = [
        asyncio.create_task(bounded_read(process.stdout)),
        asyncio.create_task(bounded_read(process.stderr)),
    ]
    try:
        async with asyncio.timeout(timeout):
            process.stdin.write(canonical_bytes(request))
            await process.stdin.drain()
            process.stdin.close()
            stdout, stderr = await asyncio.gather(*tasks)
            code = await process.wait()
            if code:
                raise RuntimeError(
                    f"child exited {code}: {stderr.decode(errors='replace')[-2000:]}"
                )
            return json.loads(stdout)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
