"""Session-local supervisor. Its caches are volatile; it never selects durable progress."""

import asyncio
import json
import os
import shutil
from pathlib import Path

from .artifacts import FileStore, Position, Tape
from .definition import Definition
from .dispatch import Dispatcher
from .inline import inline_objects
from .records import canonical_bytes
from .transport import Authority
from .warm import WarmRunner
from .workspace import Workspace

ROOT = Path("/sandbox")
SOCKET = ROOT / "bridge.sock"


class TrackingStore(FileStore):
    def __init__(self, root):
        super().__init__(root)
        self.written = {}

    def put(self, data):
        ref = super().put(data)
        self.written[ref.sha256] = ref
        return ref


class Runtime:
    def __init__(
        self,
        root,
        *,
        session,
        manifest,
        key,
        message,
        generation=1,
        provider="scripted",
        definition=None,
    ):
        self.root = Path(root)
        self.session = session
        self.manifest = manifest
        self.message = message
        self.provider = provider
        self.store = TrackingStore(self.root / "objects")
        self.tape = Tape(self.store, manifest=manifest)
        self.workdir = self.root / "workspace"
        self.workdir.mkdir(exist_ok=True)
        self.workspace = Workspace(self.workdir, self.store)
        definition = definition or Definition(json.loads(os.environ["ZETRA_AGENT"]), self.workdir)
        self.dispatcher = Dispatcher(
            definition.registry, manifest=manifest, workspace=self.workspace
        )
        self.authority = Authority(key, session, generation=generation)
        self.lock = asyncio.Lock()
        self.closing = False
        self.pending = None
        self.position = None
        self.effects = []
        self.candidate = None
        self.warm = WarmRunner(
            definition.build,
            invocation=session,
            manifest=manifest,
            message=message,
        )

    async def close(self):
        self.closing = True
        # Execute holds the command lock while awaiting its shielded task. Stop
        # that task first so shutdown can acquire the lock. No new executor can
        # start after the closing fence: admission through task creation is
        # synchronous, and queued handlers check closing under the lock.
        await self.dispatcher.close()
        async with self.lock:
            await self.warm.close()

    async def handle(self, envelope):
        action, command = self.authority.verify(envelope)
        if command["binding"]["session"] != self.session:
            raise ValueError("wrong binding")
        async with self.lock:
            if self.closing:
                raise RuntimeError("runtime is closing; replace sandbox")
            if self.warm.poisoned:
                raise RuntimeError("poisoned warm cache; replace sandbox")
            position = Position.model_validate(command["position"])
            if action == "advance":
                if any(not item[2].done() for item in self.dispatcher.tasks.values()):
                    raise ValueError("old writer still active")
                # The caller supplies the accepted Position, never a local latest pointer.
                tape = self.tape.load(position)
                if position.workspace is None:
                    raise ValueError("missing workspace")
                # Only the exact locally observed candidate can continue a warm
                # task. Every discontinuity first stops it and reconstructs cold.
                # The candidate is still not accepted until supplied by the caller.
                if position != self.position and position != self.candidate:
                    await self.warm.close()
                shutil.rmtree(self.workdir)
                self.workdir.mkdir()
                self.workspace.restore(position.workspace)
                self.pending = None
                result = await self.warm.advance(tape, command["pending"])
                self.pending = result.get("operation")
                self.position = position
                if position == self.candidate:
                    self.candidate = None
                return result
            if action == "execute":
                if command["operation"] != self.pending or position != self.position:
                    raise ValueError("execution requires the captured pinned descriptor")
                self.store.written.clear()
                before = len(self.dispatcher.tasks)
                outcome = await self.dispatcher.execute(command["operation"], command["attempt_id"])
                if len(self.dispatcher.tasks) > before:
                    self.effects.append(command["operation"]["operation_id"])
                receipt = self.tape.append_candidate(
                    position,
                    command["operation"],
                    outcome,
                    self.dispatcher.checkpoint(command["operation"]),
                )
                # Include the checkpoint on duplicate delivery even if its put was cached.
                refs = dict(self.store.written)
                refs[receipt.position.workspace.sha256] = receipt.position.workspace
                self.candidate = receipt.position
                objects = [ref.model_dump() for ref in refs.values()]
                return {
                    "receipt": receipt.model_dump(),
                    "objects": objects,
                    "inline_objects": inline_objects(self.store, objects),
                }
            raise ValueError("unsupported runtime action")


async def main(definition_factory=None):
    runtime = Runtime(
        ROOT,
        definition=definition_factory(
            ROOT / "workspace", os.environ.get("BRIDGE_PROVIDER", "scripted")
        )
        if definition_factory
        else None,
        session=os.environ["BRIDGE_SESSION"],
        manifest=os.environ["BRIDGE_MANIFEST"],
        key=bytes.fromhex(os.environ["BRIDGE_KEY"]),
        message=os.environ["BRIDGE_MESSAGE"],
        generation=int(os.environ.get("BRIDGE_GENERATION", "1")),
        provider=os.environ.get("BRIDGE_PROVIDER", "scripted"),
    )

    async def connected(reader, writer):
        try:
            data = await asyncio.wait_for(reader.readline(), timeout=5)
            if len(data) > 262144:
                raise ValueError("command too large")
            response = await runtime.handle(json.loads(data))
            encoded = canonical_bytes({"ok": response})
            if len(encoded) > 262144:
                raise ValueError("response too large")
        except Exception as exc:  # noqa: BLE001 -- boundary reports failure; never claims an outcome
            encoded = canonical_bytes({"error": type(exc).__name__, "message": str(exc)[:300]})
        writer.write(encoded + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(connected, path=SOCKET, limit=262145)
    os.chmod(SOCKET, 0o600)
    try:
        async with server:
            await server.serve_forever()
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
