"""Fixed OpenShell exec entrypoints; command text and storage roots are operator owned."""

import asyncio
import json
import sys

from .artifacts import FileStore, Ref
from .records import canonical_bytes


async def rpc():
    reader, writer = await asyncio.open_unix_connection("/sandbox/bridge.sock", limit=262145)
    data = sys.stdin.buffer.read(262145)
    if len(data) > 262144:
        raise ValueError("command too large")
    writer.write(data + b"\n")
    await writer.drain()
    result = await asyncio.wait_for(reader.readline(), timeout=35)
    writer.close()
    await writer.wait_closed()
    sys.stdout.buffer.write(result)


def main():
    mode = sys.argv[1]
    if mode == "rpc":
        asyncio.run(rpc())
        return
    store = FileStore("/sandbox/objects")
    if mode == "put-chunk":
        from .chunks import accept_chunk

        sys.stdout.buffer.write(
            canonical_bytes(accept_chunk(store, json.loads(sys.stdin.buffer.read(262145))))
        )
    elif mode == "put":
        data = sys.stdin.buffer.read(268435457)
        if len(data) > 268435456:
            raise ValueError("artifact too large")
        sys.stdout.buffer.write(canonical_bytes(store.put(data).model_dump()))
    elif mode == "get":
        ref = Ref.model_validate(json.load(sys.stdin))
        sys.stdout.buffer.write(store.get(ref))
    else:
        raise ValueError("unknown fixed command")


if __name__ == "__main__":
    main()
