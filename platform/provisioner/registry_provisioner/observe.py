"""What the provisioner reports: Agents, AgentReleases and the observed workloads."""

from __future__ import annotations

import logging

from .kube import KubePort
from .provision import API_VERSION, KINDS, Inventory, error_text, registry_annotations

log = logging.getLogger("registry_provisioner")

WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")


def observed_objects(
    kube: KubePort, namespaces: list[str], inventory: Inventory | None
) -> tuple[list[dict], list[dict]]:
    """Every Agent and AgentRelease of each namespace, and the scopes listed in full."""
    objects, complete = [], []
    for ns in namespaces:
        for kind in KINDS:
            try:
                items = kube.list(API_VERSION, kind, ns)
            except Exception as exc:
                log.warning("cannot list %s in %s: %s", kind, ns, error_text(exc))
                continue
            complete.append({"subjectKind": kind.lower(), "namespace": ns})
            for item in items:
                meta = item.get("metadata") or {}
                name = meta.get("name")
                objects.append(
                    {
                        "subjectKind": kind.lower(),
                        "namespace": ns,
                        "name": name,
                        # None: the inventory could not be read, so the answer is not known
                        "inFluxInventory": (
                            None if inventory is None else (ns, name, kind) in inventory
                        ),
                        "document": {
                            "metadata": {
                                "name": name,
                                "namespace": ns,
                                "annotations": registry_annotations(item),
                                "uid": meta.get("uid"),
                                "resourceVersion": meta.get("resourceVersion"),
                            },
                            "spec": item.get("spec") or {},
                            "status": item.get("status") or {},
                        },
                    }
                )
    return objects, complete


def observed_subjects(kube: KubePort, observe: list[dict]) -> list[dict]:
    subjects = []
    for entry in observe:
        try:
            subject = _subject(kube, entry)
        except Exception as exc:
            log.warning("cannot observe %s: %s", entry.get("name"), error_text(exc))
            continue
        if subject is not None:
            subjects.append(subject)
    return subjects


def _subject(kube: KubePort, entry: dict) -> dict | None:
    workload = entry["workload"]
    if workload.get("apiVersion") != "apps/v1" or workload.get("kind") not in WORKLOAD_KINDS:
        log.warning(
            "not observing %s: %s is not a workload kind", entry["name"], workload.get("kind")
        )
        return None
    head = {
        "subjectKind": entry["subjectKind"],
        "namespace": entry["namespace"],
        "name": entry["name"],
    }
    live = kube.get("apps/v1", workload["kind"], workload.get("namespace"), workload["name"])
    if live is None:
        return {**head, "present": False, "document": {}}
    pod = ((live.get("spec") or {}).get("template") or {}).get("spec") or {}
    spec_replicas = (live.get("spec") or {}).get("replicas")
    document = {
        "images": [c.get("image") for c in pod.get("containers") or []],
        "replicas": 1 if spec_replicas is None else spec_replicas,
        "readyReplicas": (live.get("status") or {}).get("readyReplicas") or 0,
    }
    source = entry.get("servedNameFrom")
    if entry["subjectKind"] == "model-engine" and source and source.get("kind") == "ConfigMap":
        cm = kube.get("v1", "ConfigMap", entry["namespace"], source["name"])
        value = ((cm or {}).get("data") or {}).get(source["key"])
        if value is not None:
            document["servedName"] = value
    return {**head, "present": True, "document": document}
