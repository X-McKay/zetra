import pytest

from tests.production.test_native_provisioning import NativeClient
from zetra._runtime.openshell import OpenShellSession
from zetra.worker import ActivityRouter


@pytest.mark.parametrize("value", ["secret-token", ["bad/name"], ["same", "same"], ["x"] * 9])
def test_worker_rejects_invalid_provider_configuration(value):
    with pytest.raises(ValueError, match="provider"):
        ActivityRouter(
            None,
            task_queue="q",
            runtime_id="poc",
            sandbox_client=None,
            artifacts=None,
            key=b"x" * 32,
            providers=value,
        )


async def test_native_providers_are_rebound_identically_after_worker_loss():
    client = NativeClient()
    start = {"session": "one", "message": "Inspect", "position": {}}
    for names in (["k8s-read", "inference"], ["inference", "k8s-read"]):
        remote = OpenShellSession(
            client, None, session="one", manifest="m", key=b"x" * 32, providers=names
        )
        with pytest.raises(RuntimeError, match="readiness"):
            await remote.provision(start)
    requests = [request for request, _ in client.requests]
    assert list(requests[0].spec.providers) == ["inference", "k8s-read"]
    assert requests[0].SerializeToString(deterministic=True) == requests[1].SerializeToString(
        deterministic=True
    )
    assert "MCP_ACCESS_TOKEN" not in requests[0].spec.environment
