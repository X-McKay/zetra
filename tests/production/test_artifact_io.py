import asyncio
import hashlib
import threading
from types import SimpleNamespace

import pytest

from zetra._runtime.activities import Activities
from zetra._runtime.openshell import OpenShellSession
from zetra._runtime.transport import ProtocolError


async def test_receipt_validation_runs_off_loop_under_heartbeat(monkeypatch):
    thread = threading.get_ident()
    calls = []
    monkeypatch.setattr(
        "zetra._runtime.activities.activity.heartbeat", lambda: calls.append("beat")
    )

    class Tape:
        def load(self, position):
            assert threading.get_ident() != thread
            assert calls
            raise ValueError("validation reached")

    class Transport:
        async def call(self, action, command):
            await asyncio.sleep(0)
            return {
                "previous": None,
                "operation_id": "one",
                "request_digest": "x",
                "position": {"cursor": 0},
            }

    with pytest.raises(Exception, match="validation reached"):
        await Activities(None, Transport(), Tape()).execute({"position": {"cursor": 0}})


@pytest.mark.parametrize("action", ["provision", "close"])
async def test_sdk_session_rejects_other_authority_before_sandbox_calls(action):
    original = OpenShellSession(
        None,
        None,
        session="one",
        manifest="m",
        key=b"x" * 32,
        agent_spec={"factory": "example:factory"},
    )
    labels = {
        **original.release_labels,
        "bridge-session": hashlib.sha256(b"one").hexdigest()[:32],
        "bridge-generation": "1",
    }
    calls = []

    class Client:
        def get(self, *args, **kwargs):
            return SimpleNamespace(labels=labels, id="id")

        def __getattr__(self, name):
            def called(*args, **kwargs):
                calls.append(name)
                raise AssertionError("unauthorized sandbox call")

            return called

    other = OpenShellSession(
        Client(),
        None,
        session="one",
        manifest="m",
        key=b"y" * 32,
        agent_spec={"factory": "example:factory"},
    )
    with pytest.raises(ProtocolError):
        if action == "provision":
            await other.provision({"session": "one", "position": {}, "message": ""})
        else:
            await other.close({})
    assert not calls


@pytest.mark.parametrize("action", ["advance", "execute"])
async def test_remote_artifact_store_calls_run_off_event_loop(action):
    import tempfile

    from zetra._runtime.artifacts import FileStore, Position
    from zetra._runtime.records import canonical_bytes

    thread = threading.get_ident()
    seen = []
    with tempfile.TemporaryDirectory() as root:
        local = FileStore(root)
        ref = local.put(b"payload")

        class Store:
            def get(self, reference):
                assert threading.get_ident() != thread
                seen.append("get")
                return local.get(reference)

            def put(self, data):
                assert threading.get_ident() != thread
                seen.append("put")
                return local.put(data)

        session = OpenShellSession(None, Store(), session="io", manifest="m", key=b"x" * 32)
        binding = {"session": "io", "generation": 1, "name": session.name, "sandbox_id": "id"}

        async def execute(binding, mode, data):
            if mode == "put-chunk":
                return canonical_bytes({"complete": True, "ref": ref.model_dump()})
            if mode == "get":
                return b"payload"
            return canonical_bytes(
                {"ok": {"objects": [ref.model_dump()], "inline_objects": [], "receipt": {}}}
            )

        session.checked_exec = execute
        await session.call(
            action, {"binding": binding, "position": Position(workspace=ref).model_dump()}
        )
        assert seen == (["get"] if action == "advance" else ["put"])
