"""The controller's only Kubernetes surface: a small port, and its API-server adapter."""

from __future__ import annotations

from typing import Any, Protocol

MERGE_PATCH = "application/merge-patch+json"


class KubePort(Protocol):
    def get(self, api_version: str, kind: str, namespace: str | None, name: str) -> dict | None: ...

    def list(self, api_version: str, kind: str, namespace: str | None = None) -> list[dict]: ...

    def create(self, obj: dict[str, Any]) -> dict: ...

    def replace(self, obj: dict[str, Any]) -> dict: ...

    def merge_patch(
        self, api_version: str, kind: str, namespace: str | None, name: str, patch: dict
    ) -> dict: ...

    def patch_status(
        self, api_version: str, kind: str, namespace: str, name: str, status: dict
    ) -> None: ...

    def delete(self, api_version: str, kind: str, namespace: str | None, name: str) -> None: ...


class ApiServer:
    """KubePort over the dynamic client; errors other than 404 propagate."""

    def __init__(self, api_client=None) -> None:
        from kubernetes import client, config
        from kubernetes.dynamic import DynamicClient

        if api_client is None:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            api_client = client.ApiClient()
        self._dyn = DynamicClient(api_client)

    def _res(self, api_version: str, kind: str):
        return self._dyn.resources.get(api_version=api_version, kind=kind)

    def get(self, api_version, kind, namespace, name):
        from kubernetes.dynamic.exceptions import NotFoundError

        try:
            return self._res(api_version, kind).get(name=name, namespace=namespace).to_dict()
        except NotFoundError:
            return None

    def list(self, api_version, kind, namespace=None):
        items = self._res(api_version, kind).get(namespace=namespace).to_dict()["items"]
        for item in items:  # list items omit apiVersion and kind
            item.setdefault("apiVersion", api_version)
            item.setdefault("kind", kind)
        return items

    def create(self, obj):
        ns = obj["metadata"].get("namespace")
        return self._res(obj["apiVersion"], obj["kind"]).create(body=obj, namespace=ns).to_dict()

    def replace(self, obj):
        ns = obj["metadata"].get("namespace")
        return self._res(obj["apiVersion"], obj["kind"]).replace(body=obj, namespace=ns).to_dict()

    def merge_patch(self, api_version, kind, namespace, name, patch):
        res = self._res(api_version, kind)
        return res.patch(
            body=patch, name=name, namespace=namespace, content_type=MERGE_PATCH
        ).to_dict()

    def patch_status(self, api_version, kind, namespace, name, status):
        res = self._res(api_version, kind).subresources["status"]
        res.patch(body={"status": status}, name=name, namespace=namespace, content_type=MERGE_PATCH)

    def delete(self, api_version, kind, namespace, name):
        from kubernetes.dynamic.exceptions import NotFoundError

        try:
            self._res(api_version, kind).delete(name=name, namespace=namespace)
        except NotFoundError:
            pass
