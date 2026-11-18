"""One in-memory cluster behind both Kubernetes ports: the Registry Controller's (get, list,
create, replace, merge_patch, patch_status, delete) and the provisioner's (get, list, apply).

Three things a real API server does are kept, because the programs rely on them:

- a write of an Agent fills the defaults of the Agent CRD (read from the CRD file itself);
- server-side apply merges maps by key and never touches `status`;
- a write that changes nothing keeps the resourceVersion.

Flux is played by `flux_apply` and `flux_release`: the first writes an object and lists it in
a Kustomization's inventory, the second drops it from the inventory and leaves the object, as
Flux does for an object annotated `kustomize.toolkit.fluxcd.io/prune: disabled`.
"""

from __future__ import annotations

import copy
import itertools
import uuid
from pathlib import Path
from typing import Any

import yaml

CRDS = Path(__file__).resolve().parents[3] / "deploy" / "platform" / "crds"
GROUP = "agentplatform.io"
AV = f"{GROUP}/v1alpha1"
FLUX_AV = "kustomize.toolkit.fluxcd.io/v1"

Key = tuple[str, str, str | None, str]


def _schemas() -> dict[str, dict[str, Any]]:
    """kind -> the openAPIV3Schema of the served version, for each CRD of the platform."""
    found = {}
    for path in sorted(CRDS.glob("*.yaml")):
        crd = yaml.safe_load(path.read_text())
        if crd.get("kind") != "CustomResourceDefinition":
            continue
        version = next(v for v in crd["spec"]["versions"] if v.get("served", True))
        found[crd["spec"]["names"]["kind"]] = version["schema"]["openAPIV3Schema"]
    return found


def fill_defaults(value: Any, schema: dict[str, Any]) -> None:
    """Structural defaulting as the API server does it: a missing property takes its default;
    nothing is filled inside an object that is itself missing."""
    if isinstance(value, dict):
        for name, sub in (schema.get("properties") or {}).items():
            if name not in value and "default" in sub:
                value[name] = copy.deepcopy(sub["default"])
            if name in value:
                fill_defaults(value[name], sub)
    elif isinstance(value, list) and "items" in schema:
        for item in value:
            fill_defaults(item, schema["items"])


def _merge(target: dict, patch: dict, *, null_deletes: bool) -> dict:
    for key, value in patch.items():
        if value is None and null_deletes:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value, null_deletes=null_deletes)
        else:
            target[key] = copy.deepcopy(value)
    return target


def _content(obj: dict) -> dict:
    """The object without what the server stamps on it."""
    meta = {k: v for k, v in obj["metadata"].items() if k not in ("uid", "resourceVersion")}
    return {**obj, "metadata": meta}


class Cluster:
    def __init__(self) -> None:
        self.objects: dict[Key, dict] = {}
        # (verb, field manager or "", kind, namespace, name) for every write that changed
        # something; deletes always.
        self.writes: list[tuple[str, str, str, str | None, str]] = []
        # Objects the API server refuses to show, as it does when a read is not permitted.
        self.unreadable: set[Key] = set()
        self._schemas = _schemas()
        self._rv = itertools.count(1)

    # -- storage ---------------------------------------------------------------------------

    @staticmethod
    def _key(obj: dict) -> Key:
        meta = obj["metadata"]
        return (obj["apiVersion"], obj["kind"], meta.get("namespace"), meta["name"])

    def _store(self, obj: dict, verb: str, manager: str = "") -> dict:
        obj = self.defaulted(obj)
        key = self._key(obj)
        current = self.objects.get(key)
        meta = obj["metadata"]
        if current is not None:
            meta["uid"] = current["metadata"]["uid"]
            if _content(current) == _content(obj):
                return copy.deepcopy(current)
        else:
            meta.setdefault("uid", str(uuid.uuid4()))
        meta["resourceVersion"] = str(next(self._rv))
        self.objects[key] = obj
        self.writes.append((verb, manager, obj["kind"], meta.get("namespace"), meta["name"]))
        return copy.deepcopy(obj)

    def defaulted(self, obj: dict) -> dict:
        """A document as the API server would hold it: with its CRD's defaults filled in."""
        obj = copy.deepcopy(obj)
        if obj["apiVersion"] == AV and obj["kind"] in self._schemas:
            fill_defaults(obj, self._schemas[obj["kind"]])
        return obj

    def put(self, obj: dict) -> dict:
        """Write an object as the test's own hand, status included."""
        return self._store(obj, "put")

    # -- both ports ------------------------------------------------------------------------

    def get(self, api_version: str, kind: str, namespace: str | None, name: str) -> dict | None:
        if (api_version, kind, namespace, name) in self.unreadable:
            raise RuntimeError("Forbidden")
        found = self.objects.get((api_version, kind, namespace, name))
        return copy.deepcopy(found) if found else None

    def list(self, api_version: str, kind: str, namespace: str | None = None) -> list[dict]:
        return [
            copy.deepcopy(o)
            for (av, k, ns, _), o in sorted(self.objects.items(), key=lambda kv: str(kv[0]))
            if av == api_version and k == kind and (namespace is None or ns == namespace)
        ]

    # -- the controller's port -------------------------------------------------------------

    def create(self, obj: dict) -> dict:
        if self._key(obj) in self.objects:
            raise RuntimeError("AlreadyExists")
        return self._store(obj, "create")

    def replace(self, obj: dict) -> dict:
        current = self.objects.get(self._key(obj))
        if current is None:
            raise RuntimeError("NotFound")
        if obj["metadata"].get("resourceVersion") != current["metadata"]["resourceVersion"]:
            raise RuntimeError("Conflict")
        body = copy.deepcopy(obj)
        if "status" in current:
            body["status"] = current["status"]
        return self._store(body, "replace")

    def merge_patch(self, api_version, kind, namespace, name, patch) -> dict:
        current = self.get(api_version, kind, namespace, name)
        if current is None:
            raise RuntimeError("NotFound")
        return self._store(_merge(current, patch, null_deletes=True), "patch")

    def patch_status(self, api_version, kind, namespace, name, status) -> None:
        current = self.get(api_version, kind, namespace, name)
        if current is None:
            raise RuntimeError("NotFound")
        _merge(current.setdefault("status", {}), status, null_deletes=True)
        self._store(current, "status")

    def delete(self, api_version, kind, namespace, name, *, uid=None) -> bool:
        current = self.objects.get((api_version, kind, namespace, name))
        if current is None:
            return False
        if uid and current["metadata"]["uid"] != uid:
            raise RuntimeError("Conflict")
        del self.objects[(api_version, kind, namespace, name)]
        self.writes.append(("delete", "", kind, namespace, name))
        return True

    # -- the provisioner's port ------------------------------------------------------------

    def apply(self, obj: dict, *, field_manager: str) -> dict:
        """Server-side apply with conflicts forced. Fields of other managers stay, which is
        what a map merge gives; the applier's own fields are all it sends, every time."""
        if "status" in obj:
            raise RuntimeError("apply of the main resource cannot carry status")
        current = self.get(obj["apiVersion"], obj["kind"], *self._key(obj)[2:])
        merged = obj if current is None else _merge(current, obj, null_deletes=False)
        return self._store(merged, "apply", field_manager)

    # -- Flux ------------------------------------------------------------------------------

    def _kustomization(self, namespace: str, name: str) -> dict:
        found = self.get(FLUX_AV, "Kustomization", namespace, name)
        if found is None:
            found = self.put(
                {
                    "apiVersion": FLUX_AV,
                    "kind": "Kustomization",
                    "metadata": {"namespace": namespace, "name": name},
                    "status": {"inventory": {"entries": []}},
                }
            )
        return found

    @staticmethod
    def _inventory_id(obj: dict) -> str:
        meta = obj["metadata"]
        group = obj["apiVersion"].split("/")[0] if "/" in obj["apiVersion"] else ""
        return f"{meta.get('namespace') or ''}_{meta['name']}_{group}_{obj['kind']}"

    def flux_apply(self, obj: dict, *, namespace: str = "flux-system", name: str = "apps") -> dict:
        """Flux reconciles an object from git: it is written and listed in the inventory."""
        ks = self._kustomization(namespace, name)
        entries = ks["status"]["inventory"]["entries"]
        entry = {"id": self._inventory_id(obj), "v": obj["apiVersion"].split("/")[-1]}
        if entry not in entries:
            entries.append(entry)
            self.put(ks)
        current = self.get(obj["apiVersion"], obj["kind"], *self._key(obj)[2:])
        merged = obj if current is None else _merge(current, obj, null_deletes=False)
        return self._store(merged, "apply", "kustomize-controller")

    def flux_release(
        self, obj: dict, *, namespace: str = "flux-system", name: str = "apps"
    ) -> None:
        """Git no longer lists the object and its prune is disabled: it leaves the inventory
        and stays on the cluster."""
        ks = self._kustomization(namespace, name)
        entries = ks["status"]["inventory"]["entries"]
        ks["status"]["inventory"]["entries"] = [
            e for e in entries if e["id"] != self._inventory_id(obj)
        ]
        self.put(ks)

    # -- what tests ask --------------------------------------------------------------------

    def deletes(self) -> list[tuple[str, str | None, str]]:
        return [(kind, ns, name) for verb, _, kind, ns, name in self.writes if verb == "delete"]

    def writes_by(self, manager: str, since: int = 0) -> list[tuple[str, str | None, str]]:
        return [
            (kind, ns, name)
            for verb, who, kind, ns, name in self.writes[since:]
            if verb == "apply" and who == manager
        ]
