"""T28 event path and UID-bound controller repair receipts."""

import importlib
import json

import pytest

from tests.conftest import AGENT


@pytest.fixture
def operator(monkeypatch, world):
    from registry_controller.config import ControllerConfig

    monkeypatch.setattr(ControllerConfig, "from_env", classmethod(lambda cls: world.cfg))
    module = importlib.import_module("registry_controller.operator")
    monkeypatch.setattr(module, "_reconciler", world.rec)
    return module


def test_netpol_event_repairs_exact_extra_and_receipts_uid(operator, world, caplog):
    world.add(AGENT)
    world.rec.reconcile_tenant("tenant-a")
    extra = world.kube.create(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"namespace": "tenant-a", "name": "t28-extra"},
            "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]},
        }
    )
    with caplog.at_level("INFO"):
        operator.netpol_event(namespace="tenant-a", type="ADDED", body=extra)
    assert world.kube.get("networking.k8s.io/v1", "NetworkPolicy", "tenant-a", "t28-extra") is None
    receipts = [json.loads(r.message) for r in caplog.records if r.message.startswith("{")]
    assert len(receipts) == 1
    assert receipts[0] == {
        "event": "tenant_network_policy_deleted",
        "tenant": "tenant-a",
        "name": "t28-extra",
        "uid": extra["metadata"]["uid"],
        "resource_version": extra["metadata"]["resourceVersion"],
    }


@pytest.mark.parametrize("namespace", [None, "default", "tenant-b"])
def test_netpol_event_does_not_reconcile_unmanaged_namespace(operator, world, namespace):
    world.kube.writes.clear()
    operator.netpol_event(namespace=namespace)
    assert not world.kube.writes


def test_no_controller_receipt_when_policy_disappeared_before_delete(
    operator, world, monkeypatch, caplog
):
    world.add(AGENT)
    world.rec.reconcile_tenant("tenant-a")
    world.kube.create(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"namespace": "tenant-a", "name": "t28-extra"},
            "spec": {"podSelector": {}, "egress": [{}]},
        }
    )
    monkeypatch.setattr(world.kube, "delete", lambda *args, **kwargs: False)
    with caplog.at_level("INFO"):
        operator.netpol_event(namespace="tenant-a")
    assert not any("tenant_network_policy_deleted" in record.message for record in caplog.records)


def test_api_delete_uid_precondition_and_404_acknowledgement():
    from types import SimpleNamespace as NS
    from kubernetes.dynamic.exceptions import NotFoundError
    from kubernetes.client.exceptions import ApiException
    from registry_controller.kube import ApiServer

    api = ApiServer.__new__(ApiServer)
    calls = []
    api._res = lambda *args: NS(delete=lambda **kwargs: calls.append(kwargs))
    assert (
        api.delete("networking.k8s.io/v1", "NetworkPolicy", "tenant-a", "extra", uid="exact")
        is True
    )
    assert calls == [
        {
            "name": "extra",
            "namespace": "tenant-a",
            "body": {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": "exact"},
            },
        }
    ]

    def absent(**kwargs):
        raise NotFoundError(ApiException(status=404, reason="absent"))

    api._res = lambda *args: NS(delete=absent)
    assert (
        api.delete("networking.k8s.io/v1", "NetworkPolicy", "tenant-a", "extra", uid="exact")
        is False
    )


def test_uid_conflict_has_no_controller_deletion_receipt(operator, world, monkeypatch, caplog):
    world.add(AGENT)
    world.rec.reconcile_tenant("tenant-a")
    world.kube.create(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"namespace": "tenant-a", "name": "t28-extra"},
            "spec": {"podSelector": {}, "egress": [{}]},
        }
    )

    def conflict(*args, **kwargs):
        raise RuntimeError("Conflict")

    monkeypatch.setattr(world.kube, "delete", conflict)
    with caplog.at_level("INFO"), pytest.raises(RuntimeError, match="Conflict"):
        operator.netpol_event(namespace="tenant-a")
    assert not any("tenant_network_policy_deleted" in record.message for record in caplog.records)
