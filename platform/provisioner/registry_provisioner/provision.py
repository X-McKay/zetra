"""Deciding and applying: the table of projection.md section 5."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .kube import KubePort

log = logging.getLogger("registry_provisioner")

PREFIX = "registry.agentplatform.io/"
GENERATION = PREFIX + "generation"
API_VERSION = "agentplatform.io/v1alpha1"
KINDS = ("Agent", "AgentRelease")
FLUX_API_VERSION = "kustomize.toolkit.fluxcd.io/v1"
FLUX_GROUP = "agentplatform.io"
STATE_NAME = "registry-provisioner-state"
STATE_KEY = "lastGeneration"

Inventory = set[tuple[str, str, str]]  # (namespace, name, kind)


class InventoryError(Exception):
    pass


@dataclass(frozen=True)
class Result:
    deployment: str
    kind: str
    namespace: str
    name: str
    generation: int
    result: str
    message: str = ""

    def wire(self) -> dict:
        return {
            "deployment": self.deployment,
            "kind": self.kind,
            "namespace": self.namespace,
            "name": self.name,
            "generation": self.generation,
            "result": self.result,
            "message": self.message,
        }


def registry_annotations(obj: dict) -> dict:
    annotations = (obj.get("metadata") or {}).get("annotations") or {}
    return {k: v for k, v in annotations.items() if k.startswith(PREFIX)}


def error_text(exc: Exception) -> str:
    return (str(exc) or type(exc).__name__)[:500]


def read_last_generation(kube: KubePort, namespace: str) -> int:
    cm = kube.get("v1", "ConfigMap", namespace, STATE_NAME)
    try:
        return max(0, int(((cm or {}).get("data") or {}).get(STATE_KEY, 0)))
    except (TypeError, ValueError):
        log.warning("%s holds no usable %s; reading 0", STATE_NAME, STATE_KEY)
        return 0


def store_last_generation(
    kube: KubePort, namespace: str, stored: int, answered: int, field_manager: str
) -> int:
    """Store the higher of the two numbers; a lower answer never lowers it."""
    value = max(stored, answered)
    cm = kube.get("v1", "ConfigMap", namespace, STATE_NAME)
    if cm is None or value > stored:
        kube.apply(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": STATE_NAME, "namespace": namespace},
                "data": {STATE_KEY: str(value)},
            },
            field_manager=field_manager,
        )
    return value


def read_inventories(kube: KubePort, refs: list[dict]) -> Inventory:
    found: Inventory = set()
    for ref in refs:
        ns, name = ref.get("namespace"), ref.get("name")
        try:
            ks = kube.get(FLUX_API_VERSION, "Kustomization", ns, name)
        except Exception as exc:
            raise InventoryError(
                f"cannot read Kustomization {ns}/{name}: {error_text(exc)}"
            ) from None
        if ks is None:
            raise InventoryError(f"Kustomization {ns}/{name} does not exist")
        entries = ((ks.get("status") or {}).get("inventory") or {}).get("entries") or []
        for entry in entries:
            parts = str(entry.get("id", "")).split("_")
            if len(parts) == 4 and parts[2] == FLUX_GROUP and parts[3] in KINDS:
                found.add((parts[0], parts[1], parts[3]))
    return found


def _generation(live: dict) -> int | None:
    value = registry_annotations(live).get(GENERATION)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _equal(live: dict, desired: dict) -> bool:
    """The spec, and the annotations under the registry prefix; nothing else counts."""
    return (live.get("spec") or {}) == (desired.get("spec") or {}) and registry_annotations(
        live
    ) == registry_annotations(desired)


def _decide(live: dict | None, desired: dict, generation: int) -> tuple[str, str]:
    """(result, message) for an object that is on the cluster or not, and not in Flux."""
    if live is None:
        return "applied", ""
    live_generation = _generation(live)
    if live_generation is None:
        return "applied", "adopted: the live object carried no generation"
    if live_generation > generation:
        return "refused-older", f"the cluster holds generation {live_generation}"
    if live_generation < generation:
        return "applied", ""
    if _equal(live, desired):
        return "unchanged", ""
    return "applied", "repaired: the live object had been changed"


def project_object(
    kube: KubePort,
    entry: dict,
    deployment: str,
    *,
    namespaces: set[str],
    inventory: Inventory,
    field_manager: str,
) -> Result:
    kind, ns, name = entry.get("kind"), entry.get("namespace"), entry.get("name")
    generation = entry.get("generation", 0)
    doc = entry.get("document") or {}

    def result(outcome: str, message: str = "") -> Result:
        return Result(deployment, str(kind), str(ns), str(name), generation, outcome, message)

    meta = doc.get("metadata") or {}
    if kind not in KINDS:
        return result("error", f"kind {kind} is not one the provisioner writes")
    if ns not in namespaces:
        return result("error", f"namespace {ns} is not one this cluster's answer lists")
    if (doc.get("apiVersion"), doc.get("kind"), meta.get("namespace"), meta.get("name")) != (
        API_VERSION,
        kind,
        ns,
        name,
    ):
        return result("error", "the document does not match the object it describes")
    if (ns, name, kind) in inventory:
        return result("refused-flux-inventory", "the object is in a Flux inventory")
    try:
        live = kube.get(API_VERSION, kind, ns, name)
        outcome, message = _decide(live, doc, generation)
        if outcome == "applied":
            written = kube.apply(doc, field_manager=field_manager)
            if not _equal(written, doc):
                return result(
                    "error",
                    "after the write the object still differs: another writer owns fields "
                    "the registry's document does not have",
                )
    except Exception as exc:
        return result("error", error_text(exc))
    return result(outcome, message)
