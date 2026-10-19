"""Real worker process for abrupt restart conformance."""

import asyncio
import json
import sys

from temporalio.client import Client
from temporalio.worker import Worker

from zetra._runtime.activities import Activities
from zetra._runtime.artifacts import FileStore, Tape
from zetra._runtime.temporal import SessionWorkflow
from zetra._runtime.transport import Authority, HttpTransport


async def main():
    config = json.loads(sys.stdin.readline())
    transport = HttpTransport(config["url"], Authority(b"x" * 32, config["session"], generation=1))

    class Lifecycle:
        async def provision(self, start):
            return config["binding"]

        async def wait_ready(self, binding):
            return binding

        async def close(self, command):
            return await transport.call("close", command)

    activities = Activities(
        Lifecycle(), transport, Tape(FileStore(config["objects"]), manifest="probe-v1")
    )
    client = await Client.connect(config["endpoint"])
    async with Worker(
        client,
        task_queue=config["queue"],
        workflows=[SessionWorkflow],
        activities=activities.registered(),
    ):
        print("READY", flush=True)
        await asyncio.Future()


asyncio.run(main())
