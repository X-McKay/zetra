"""Trusted local test service. Not a production sandbox implementation."""

import asyncio
import json
import shutil

from aiohttp import web
from google.adk.models.llm_request import LlmRequest

from tests.fixtures.cases import FixtureCase, ScriptedModel, build_agent
from tests.fixtures.remote import fresh_advance
from zetra._runtime.artifacts import FileStore, Position, Tape
from zetra._runtime.dispatch import Dispatcher
from zetra._runtime.records import decode_model, encode_model
from zetra._runtime.transport import Authority, HttpTransport
from zetra._runtime.workspace import Workspace


class FixtureRuntime:
    def __init__(self, root, session):
        self.root = root
        self.session = session
        self.case = FixtureCase()
        self.delay_next = 0
        self.started = asyncio.Event()
        self.store = FileStore(root / "objects")
        self.tape = Tape(self.store, manifest="probe-v1")
        self.workspace_root = root / "workspace"
        self.workspace_root.mkdir()
        self.workspace = Workspace(self.workspace_root, self.store)
        self.initial = self.tape.empty(self.workspace.capture())
        model = ScriptedModel(self.case)

        async def execute_model(payload, state, key):
            self.started.set()
            if self.delay_next:
                delay = self.delay_next
                self.delay_next = 0
                await asyncio.sleep(delay)
            # JSON decode unfreezes the canonical readonly input into a fresh LlmRequest.
            request = decode_model(LlmRequest, json.loads(json.dumps(thaw(payload))))
            responses = [r async for r in model.generate_content_async(request)]
            return encode_model(responses[0])

        registry = {"fixture": execute_model}
        for function in build_agent(self.case).tools:

            async def execute_tool(args, state, key, fn=function):
                result = await fn(**dict(args))
                if fn.__name__ == "write_report":
                    (self.workspace_root / "report.md").write_text(args["content"])
                return result

            registry[function.__name__] = execute_tool
        self.dispatcher = Dispatcher(registry, manifest="probe-v1", workspace=self.workspace)
        self.authority = Authority(b"x" * 32, session, generation=1)
        self.fail_after_candidate = False
        self.fail_at = None
        self.candidates = []

    async def provision(self, start):
        return {"session": self.session, "generation": 1, "position": self.initial.model_dump()}

    async def wait_ready(self, binding):
        return binding

    async def handle(self, request):
        action, payload = self.authority.verify(await request.json())
        if action == "close":
            return web.json_response(await self.close(payload))
        if payload["binding"]["session"] != self.session:
            raise ValueError("wrong binding")
        position = Position.model_validate(payload["position"])
        if action == "advance":
            result = await fresh_advance(
                {"tape": self.tape.load(position), "expected_pending": payload["pending"]}
            )
            if "error" in result:
                raise ValueError(result["error"])
            result = {k: v for k, v in result.items() if k in ("status", "operation", "result")}
        elif action == "execute":
            if self.fail_at == "before_dispatch":
                raise web.HTTPServiceUnavailable()
            outcome = await self.dispatcher.execute(payload["operation"], payload["attempt_id"])
            if self.fail_at == "after_effect":
                raise web.HTTPServiceUnavailable()
            receipt = self.tape.append_candidate(
                position,
                payload["operation"],
                outcome,
                self.dispatcher.checkpoint(payload["operation"]),
            )
            self.candidates.append(receipt)
            if self.fail_after_candidate or self.fail_at == "after_candidate":
                raise web.HTTPServiceUnavailable(text="completion lost")
            result = receipt.model_dump()
        else:
            raise ValueError("unsupported action")
        return web.json_response(result)

    async def start(self):
        app = web.Application(client_max_size=262144)
        app.router.add_post("/", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return HttpTransport(f"http://127.0.0.1:{port}/", self.authority)

    async def close(self, command):
        await self.dispatcher.close()
        shutil.rmtree(self.workspace_root, ignore_errors=True)
        return {
            "cleanup": "DELETED" if not self.workspace_root.exists() else "REMEDIATION_REQUIRED"
        }

    async def stop(self):
        await self.dispatcher.close()
        await self.runner.cleanup()


def thaw(value):
    from collections.abc import Mapping

    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value
