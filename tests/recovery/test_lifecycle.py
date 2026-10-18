"""Lifecycle races: an accepted binding may lag resources already created."""

import asyncio
import threading
from types import SimpleNamespace

import grpc
import pytest

# Fixture sandbox transport.
from tests.fixtures.sandbox import OpenShellSession


class Missing(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.NOT_FOUND


class FakeClient:
    def __init__(self):
        self.resources = {}
        self.creating = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def get(self, name, **kwargs):
        if name not in self.resources:
            raise Missing()
        return self.resources[name]

    def create(self, name, labels, **kwargs):
        self.creating.set()
        assert self.release.wait(5)
        ref = SimpleNamespace(name=name, id=name, labels=labels)
        self.resources[name] = ref
        return ref

    def wait_ready(self, name, **kwargs):
        raise RuntimeError("injected readiness failure")

    def list(self, **kwargs):
        return SimpleNamespace(all=lambda: list(self.resources.values()))

    def delete(self, name, **kwargs):
        self.resources.pop(name, None)

    def stop(self, name, **kwargs):
        return self.get(name)

    def wait_deleted(self, name, **kwargs):
        assert name not in self.resources


def remote(client):
    return OpenShellSession(client, None, session="race", manifest="demo-v1", key=b"x" * 32)


async def test_failed_replacement_cleanup_reconciles_unaccepted_generation():
    client = FakeClient()
    session = remote(client)
    start = {"session": "race", "message": "Inspect", "position": {}}
    with pytest.raises(RuntimeError, match="readiness"):
        await session.provision(start)
    old = {"session": "race", "generation": 1, "name": session.name, "sandbox_id": session.name}
    for _ in range(3):
        with pytest.raises(RuntimeError, match="readiness"):
            await session.replace({"start": start, "position": {}, "binding": old})
    assert len(client.resources) == 1
    assert (await session.close({"start": start, "binding": old}))["cleanup"] == "DELETED"
    assert not client.resources


class RpcFailure(grpc.RpcError):
    def __init__(self, status):
        self.status = status

    def code(self):
        return self.status


async def cleanup_fixture():
    client = FakeClient()
    session = remote(client)
    with pytest.raises(RuntimeError, match="readiness"):
        await session.provision({"session": "race", "message": "Inspect", "position": {}})
    binding = {"session": "race", "generation": 1, "name": session.name, "sandbox_id": session.name}
    return client, session, binding


async def test_orderly_stop_precedes_delete_but_never_replaces_confirmation():
    client, session, binding = await cleanup_fixture()
    calls = []
    confirmation = threading.Event()
    release = threading.Event()
    original_delete = client.delete

    def stop(name, **kwargs):
        calls.append("stop")
        return client.get(name)

    def delete(name, **kwargs):
        calls.append("delete")
        original_delete(name, **kwargs)

    def wait_deleted(name, *, expected_sandbox_id, **kwargs):
        assert expected_sandbox_id == binding["sandbox_id"]
        confirmation.set()
        assert release.wait(5)

    client.stop, client.delete, client.wait_deleted = stop, delete, wait_deleted
    task = asyncio.create_task(session.close({"binding": binding}))
    try:
        async with asyncio.timeout(5):
            while not confirmation.is_set():
                await asyncio.sleep(0.01)
        assert calls == ["stop", "delete"]
        assert not task.done(), "stop/delete acknowledgements are not deletion confirmation"
    finally:
        release.set()
        result = await task
    assert result == {"cleanup": "DELETED"}


@pytest.mark.parametrize(
    "status",
    [
        grpc.StatusCode.FAILED_PRECONDITION,
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.UNAVAILABLE,
    ],
)
async def test_stop_failure_falls_back_to_confirmed_deletion(status):
    client, session, binding = await cleanup_fixture()

    def stop(*args, **kwargs):
        raise RpcFailure(status)

    client.stop = stop
    assert await session.close({"binding": binding}) == {"cleanup": "DELETED"}
    assert not client.resources


@pytest.mark.parametrize("changed", ["id", "owner"])
async def test_stop_does_not_authorize_deleting_a_changed_identity(changed):
    client, session, binding = await cleanup_fixture()

    def stop(name, **kwargs):
        old = client.get(name)
        client.resources[name] = SimpleNamespace(
            name=name,
            id="other-instance" if changed == "id" else old.id,
            labels={"bridge-session": "other-session"} if changed == "owner" else old.labels,
        )
        return old  # Even a successful stop response is stale by the next RPC.

    client.stop = stop
    assert await session.close({"binding": binding}) == {"cleanup": "REMEDIATION_REQUIRED"}
    assert binding["name"] in client.resources


async def test_stop_permission_failure_does_not_claim_cleanup():
    client, session, binding = await cleanup_fixture()

    def stop(*args, **kwargs):
        raise RpcFailure(grpc.StatusCode.PERMISSION_DENIED)

    client.stop = stop
    with pytest.raises(RpcFailure):
        await session.close({"binding": binding})
    assert binding["name"] in client.resources


async def test_stop_success_and_delete_ack_cannot_mask_confirmation_timeout():
    client, session, binding = await cleanup_fixture()

    def wait_deleted(*args, **kwargs):
        raise TimeoutError("instance deletion is unconfirmed")

    client.wait_deleted = wait_deleted
    with pytest.raises(TimeoutError, match="unconfirmed"):
        await session.close({"binding": binding})


async def test_cancelled_create_settles_before_terminal_cleanup():
    client = FakeClient()
    client.release.clear()
    session = remote(client)
    start = {"session": "race", "message": "Inspect", "position": {}}
    task = asyncio.create_task(session.provision(start))
    while not client.creating.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done(), "creation must settle before cancellation completes"
    client.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await session.close({"start": start, "binding": None}))["cleanup"] == "DELETED"
    assert not client.resources


async def test_terminal_cleanup_fences_a_delayed_initial_lookup():
    from zetra._runtime.transport import ProtocolError

    client = FakeClient()
    entered = threading.Event()
    released = threading.Event()
    original_get = client.get

    def delayed_get(name, **kwargs):
        entered.set()
        assert released.wait(5)
        return original_get(name, **kwargs)

    client.get = delayed_get
    session = remote(client)
    start = {"session": "race", "message": "Inspect", "position": {}}
    task = asyncio.create_task(session.provision(start))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    assert (await session.close({"start": start, "binding": None}))["cleanup"] == "DELETED"
    released.set()
    with pytest.raises(ProtocolError, match="closing"):
        await task
    assert not client.resources
