"""The transport must replay one create and never refresh its admission window."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from tests.recovery.test_lifecycle import FakeClient
from zetra._runtime.openshell import OpenShellSession
from zetra._runtime.transport import ProtocolError


class NativeClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.requests = []
        self._timeout = 30.0
        self._stub = SimpleNamespace(CreateSandbox=self.native_create)

    def native_create(self, request, *, timeout):
        self.requests.append((request, timeout))
        return super().native_create(request, timeout=timeout)


def remote(client, session="one"):
    return OpenShellSession(client, None, session=session, manifest="m", key=b"x" * 32)


async def test_fresh_adapter_replays_identical_native_create_across_recovery():
    client = NativeClient()
    start = {"session": "one", "message": "Inspect", "position": {}}
    for _ in range(2):
        with pytest.raises(RuntimeError, match="readiness"):
            await remote(client).provision(start)
    assert len(client.requests) == 2, "existing-name lookup must not bypass native admission"
    first, second = [request for request, _ in client.requests]
    assert UUID(first.request_id).int != 0
    assert first.SerializeToString(deterministic=True) == second.SerializeToString(
        deterministic=True
    )
    assert all(0 < timeout <= 30 for _, timeout in client.requests)


async def test_native_create_ids_separate_session_and_generation():
    client = NativeClient()
    for session, generation in [("one", 1), ("one", 2), ("two", 1)]:
        with pytest.raises(RuntimeError, match="readiness"):
            await remote(client, session).provision(
                {"session": session, "generation": generation, "message": "Inspect", "position": {}}
            )
    ids = [request.request_id for request, _ in client.requests]
    assert len(ids) == len(set(ids)) == 3
    assert all(UUID(value).int for value in ids)


async def test_recovery_does_not_renew_expired_original_activity_window(monkeypatch):
    scheduled = datetime.now(UTC) - timedelta(hours=1)
    monkeypatch.setattr("temporalio.activity.in_activity", lambda: True, raising=False)
    monkeypatch.setattr(
        "temporalio.activity.info",
        lambda: SimpleNamespace(
            scheduled_time=scheduled, current_attempt_scheduled_time=datetime.now(UTC)
        ),
        raising=False,
    )
    client = NativeClient()
    for _ in range(2):
        with pytest.raises(ProtocolError, match="admission window"):
            await remote(client).provision({"session": "one", "message": "Inspect", "position": {}})
    assert not client.resources and not client.requests
