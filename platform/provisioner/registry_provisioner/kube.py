"""The provisioner's only Kubernetes surface: a small port, and its API-server adapter.

The port has no delete method, on purpose.
"""

from __future__ import annotations

from typing import Any, Protocol


class KubePort(Protocol):
    def get(self, api_version: str, kind: str, namespace: str | None, name: str) -> dict | None: ...

    def list(self, api_version: str, kind: str, namespace: str | None = None) -> list[dict]: ...

    def apply(self, obj: dict[str, Any], *, field_manager: str) -> dict:
        """Server-side apply, forcing conflicts."""
        ...


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

    def apply(self, obj, *, field_manager):
        meta = obj["metadata"]
        return (
            self._res(obj["apiVersion"], obj["kind"])
            .server_side_apply(
                body=obj,
                name=meta["name"],
                namespace=meta.get("namespace"),
                field_manager=field_manager,
                force_conflicts=True,
            )
            .to_dict()
        )
