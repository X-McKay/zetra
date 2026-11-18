import pytest

from tests.fakes import FakeKube, FakeRegistry, example

ENV = {
    "PROVISIONER_CLUSTER": "kubani",
    "PROVISIONER_REGISTRY_URL": "https://registry.test",
    "PROVISIONER_NAMESPACE": "provisioner",
}


@pytest.fixture
def env(tmp_path):
    return {**ENV, "PROVISIONER_HEARTBEAT_FILE": str(tmp_path / "heartbeat")}


@pytest.fixture
def kube():
    fake = FakeKube()
    fake.put_kustomization()  # the one the example names, with an empty inventory
    return fake


@pytest.fixture
def desired():
    return example("desired.json")


@pytest.fixture
def registry(desired):
    return FakeRegistry(desired)
