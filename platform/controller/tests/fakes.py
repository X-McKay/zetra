"""In-memory fakes for the controller's three ports."""

from __future__ import annotations

import copy
import itertools
import uuid
from typing import Any


def _merge(target: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
    return target


class FakeKube:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str, str | None, str], dict] = {}
        self.writes: list[tuple[str, str, str]] = []
        self._rv = itertools.count(1)

    def _key(self, api_version, kind, namespace, name):
        return (api_version, kind, namespace, name)

    def put(self, obj: dict) -> dict:
        obj = copy.deepcopy(obj)
        meta = obj["metadata"]
        meta.setdefault("uid", str(uuid.uuid4()))
        meta["resourceVersion"] = str(next(self._rv))
        self.objects[
            self._key(obj["apiVersion"], obj["kind"], meta.get("namespace"), meta["name"])
        ] = obj
        return obj

    def obj(self, api_version, kind, namespace, name) -> dict | None:
        return self.objects.get(self._key(api_version, kind, namespace, name))

    # KubePort
    def get(self, api_version, kind, namespace, name):
        found = self.obj(api_version, kind, namespace, name)
        return copy.deepcopy(found) if found else None

    def list(self, api_version, kind, namespace=None):
        return [
            copy.deepcopy(o)
            for (av, k, ns, _), o in sorted(self.objects.items(), key=lambda kv: str(kv[0]))
            if av == api_version and k == kind and (namespace is None or ns == namespace)
        ]

    def create(self, obj):
        meta = obj["metadata"]
        if self.obj(obj["apiVersion"], obj["kind"], meta.get("namespace"), meta["name"]):
            raise RuntimeError("AlreadyExists")
        self.writes.append(("create", obj["kind"], meta["name"]))
        return self.put(obj)

    def replace(self, obj):
        meta = obj["metadata"]
        cur = self.obj(obj["apiVersion"], obj["kind"], meta.get("namespace"), meta["name"])
        assert cur is not None and meta.get("resourceVersion") == cur["metadata"]["resourceVersion"]
        self.writes.append(("replace", obj["kind"], meta["name"]))
        body = copy.deepcopy(obj)
        if "status" in cur:
            body["status"] = cur["status"]
        return self.put(body)

    def merge_patch(self, api_version, kind, namespace, name, patch):
        cur = self.obj(api_version, kind, namespace, name)
        if cur is None:
            raise RuntimeError("NotFound")
        self.writes.append(("patch", kind, name))
        _merge(cur, copy.deepcopy(patch))
        return copy.deepcopy(cur)

    def patch_status(self, api_version, kind, namespace, name, status):
        cur = self.obj(api_version, kind, namespace, name)
        if cur is None:
            raise RuntimeError("NotFound")
        _merge(cur.setdefault("status", {}), copy.deepcopy(status))

    def delete(self, api_version, kind, namespace, name):
        if self.objects.pop(self._key(api_version, kind, namespace, name), None) is not None:
            self.writes.append(("delete", kind, name))


class FakeIdp:
    def __init__(self) -> None:
        self.clients: dict[str, Any] = {}
        self.fail = False

    def ensure_agent(self, spec):
        if self.fail:
            raise ConnectionError("idp down")
        self.clients[spec.client_id] = spec
        return str(uuid.uuid5(uuid.NAMESPACE_URL, spec.client_id))

    def delete_agent(self, client_id, claims_scope):
        self.clients.pop(client_id, None)


class FakeVerifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []
        self.signers: list = []

    def verify(self, image, manifest_sha256, policy_sha256, signer=None):
        self.calls.append(image)
        self.signers.append(signer)
        return self.ok
