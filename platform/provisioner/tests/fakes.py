"""In-memory fakes for the two ports."""

from __future__ import annotations

import copy
import itertools
import json
import threading
import uuid
from pathlib import Path

from registry_provisioner.registry import RegistryError

EXAMPLES = Path(__file__).resolve().parents[2] / "registry" / "contract" / "examples"
AV = "agentplatform.io/v1alpha1"
PREFIX = "registry.agentplatform.io/"


def example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


def _merge(target: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class FakeKube:
    def __init__(self) -> None:
        self.objects: dict[tuple, dict] = {}
        self.writes: list[tuple[str, str, str]] = []
        self.managers: list[str] = []
        self.log: list[tuple] = []  # writes and session calls, in the order they happened
        self.fail_apply: set[str] = set()  # object names whose apply raises
        self.fail_apply_all = False
        self.fail_list: set[str] = set()  # kinds whose list raises
        self._rv = itertools.count(1000)

    @staticmethod
    def _key(api_version, kind, namespace, name):
        return (api_version, kind, namespace, name)

    def put(self, obj: dict) -> dict:
        obj = copy.deepcopy(obj)
        meta = obj["metadata"]
        meta.setdefault("uid", str(uuid.uuid4()))
        meta["resourceVersion"] = str(next(self._rv))
        key = self._key(obj["apiVersion"], obj["kind"], meta.get("namespace"), meta["name"])
        self.objects[key] = obj
        return obj

    def obj(self, api_version, kind, namespace, name) -> dict | None:
        return self.objects.get(self._key(api_version, kind, namespace, name))

    def get(self, api_version, kind, namespace, name):
        found = self.obj(api_version, kind, namespace, name)
        return copy.deepcopy(found) if found else None

    def list(self, api_version, kind, namespace=None):
        if kind in self.fail_list:
            raise RuntimeError("list refused")
        return [
            copy.deepcopy(o)
            for (av, k, ns, _), o in sorted(self.objects.items(), key=lambda kv: str(kv[0]))
            if av == api_version and k == kind and (namespace is None or ns == namespace)
        ]

    def apply(self, obj, *, field_manager):
        meta = obj["metadata"]
        if self.fail_apply_all or meta["name"] in self.fail_apply:
            raise RuntimeError(f"apply of {meta['name']} refused")
        self.managers.append(field_manager)
        self.log.append(
            ("write", obj["kind"], meta["name"], (obj.get("spec") or {}).get("approval"))
        )
        self.writes.append((obj["kind"], meta.get("namespace"), meta["name"]))
        key = self._key(obj["apiVersion"], obj["kind"], meta.get("namespace"), meta["name"])
        cur = self.objects.get(key)
        if cur is None:
            return self.put(obj)
        merged = copy.deepcopy(cur)
        _merge(merged, copy.deepcopy(obj))  # maps by key; lists and scalars replaced
        return self.put(merged)

    # test helpers
    def state(self, namespace: str = "provisioner") -> dict | None:
        return self.obj("v1", "ConfigMap", namespace, "registry-provisioner-state")

    def stored(self, namespace: str = "provisioner") -> int | None:
        cm = self.state(namespace)
        return int(cm["data"]["lastGeneration"]) if cm else None

    def set_stored(self, value: int, namespace: str = "provisioner") -> None:
        self.put(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "registry-provisioner-state", "namespace": namespace},
                "data": {"lastGeneration": str(value)},
            }
        )

    def put_kustomization(self, namespace="flux-system", name="apps", ids=()) -> None:
        self.put(
            {
                "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
                "kind": "Kustomization",
                "metadata": {"namespace": namespace, "name": name},
                "status": {"inventory": {"entries": [{"id": i, "v": "v1alpha1"} for i in ids]}},
            }
        )

    def put_deployment(self, namespace, name, images, replicas=1, ready=1) -> None:
        self.put(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"namespace": namespace, "name": name},
                "spec": {
                    "replicas": replicas,
                    "template": {
                        "spec": {
                            "containers": [
                                {"name": f"c{i}", "image": image} for i, image in enumerate(images)
                            ]
                        }
                    },
                },
                "status": {"readyReplicas": ready},
            }
        )


class FakeRegistry:
    def __init__(self, desired: dict | None = None) -> None:
        self.answer = desired
        self.desired_calls: list[int] = []
        self.reports: list[dict] = []
        self.desired_error: RegistryError | None = None
        self.report_error: RegistryError | None = None
        self.on_desired = None

    def desired(self, last_generation: int) -> dict:
        self.desired_calls.append(last_generation)
        if self.on_desired:
            self.on_desired(len(self.desired_calls))
        if self.desired_error:
            raise self.desired_error
        return copy.deepcopy(self.answer)

    def report(self, observations: dict) -> dict:
        if self.report_error:
            raise self.report_error
        self.reports.append(copy.deepcopy(observations))
        return {"mode": "normal", "generation": 0, "stored": 0}


class FakeSessions:
    """The tenant's session interface; calls are also put on the kube's log, for order."""

    def __init__(self, kube: FakeKube | None = None) -> None:
        self.kube = kube
        self.calls: list[tuple[str, str, str, str]] = []  # tenant, agent, release, reason
        self.error: Exception | None = None
        self.cancelled = 2

    def cancel_release(self, tenant, agent, release, reason) -> int:
        self.calls.append((tenant, agent, release, reason))
        if self.kube is not None:
            self.kube.log.append(("cancel", release))
        if self.error:
            raise self.error
        return self.cancelled


class Stopper(threading.Event):
    """A stop flag that never really waits."""

    def wait(self, timeout=None):
        return self.is_set()
