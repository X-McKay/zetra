"""Small strict manifest contract. Unknown fields fail rather than silently disappear."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ContractError(ValueError):
    """A manifest or evidence fails the contract."""


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate keys: otherwise a reviewed value could be shadowed."""


def _mapping(loader: UniqueLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ContractError("YAML keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def mapping(value: Any, keys: set[str], at: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        got = set(value) if isinstance(value, dict) else set()
        raise ContractError(
            f"{at}: expected keys {sorted(keys)}, missing {sorted(keys - got)}, unknown {sorted(got - keys)}"
        )
    return value


def string(value: Any, at: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ContractError(f"{at}: expected nonempty string of at most 256 characters")
    return value


def choice(value: Any, choices: set[str], at: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ContractError(f"{at}: expected one of {sorted(choices)}")
    return value


def number(value: Any, at: str, ceiling: float, integer: bool = False) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= ceiling
    ):
        raise ContractError(f"{at}: expected finite positive number <= {ceiling}")
    if integer and type(value) is not int:
        raise ContractError(f"{at}: expected integer")
    return value


@dataclass(frozen=True)
class Manifest:
    name: str
    owner: str
    business_function: str
    entrypoint: str
    capabilities: tuple[str, ...]
    data_classification: str
    autonomy: str
    execution: str
    max_steps: int
    max_seconds: int
    max_cost_usd: float
    raw: dict

    @classmethod
    def parse(cls, raw: Any) -> Manifest:
        mapping(raw, {"apiVersion", "kind", "metadata", "spec"}, "manifest")
        if raw["apiVersion"] != "zetra.dev/v1alpha1" or raw["kind"] != "Agent":
            raise ContractError("Unsupported apiVersion/kind")
        meta = mapping(raw["metadata"], {"name", "owner", "businessFunction"}, "metadata")
        name = string(meta["name"], "metadata.name")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", name):
            raise ContractError("metadata.name must be a DNS-safe lowercase label <= 48 characters")
        spec = mapping(
            raw["spec"],
            {
                "entrypoint",
                "capabilities",
                "dataClassification",
                "autonomy",
                "execution",
                "budgets",
            },
            "spec",
        )
        entrypoint = string(spec["entrypoint"], "spec.entrypoint")
        if not re.fullmatch(r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*:[a-zA-Z_]\w*", entrypoint):
            raise ContractError("entrypoint must be dotted.module:factory")
        caps = spec["capabilities"]
        if (
            not isinstance(caps, list)
            or len(caps) > 64
            or len(set(c for c in caps if isinstance(c, str))) != len(caps)
        ):
            raise ContractError("capabilities must be a unique list of strings")
        for cap in caps:
            if not isinstance(cap, str) or not re.fullmatch(
                r"[a-z][a-z0-9_-]*\.[a-z][a-z0-9_-]*", cap
            ):
                raise ContractError("Capabilities use resource.operation syntax")
        budget = mapping(spec["budgets"], {"maxSteps", "maxSeconds", "maxCostUsd"}, "spec.budgets")
        return cls(
            name,
            string(meta["owner"], "metadata.owner"),
            string(meta["businessFunction"], "metadata.businessFunction"),
            entrypoint,
            tuple(caps),
            choice(
                spec["dataClassification"],
                {"public", "internal", "confidential", "restricted", "unknown"},
                "dataClassification",
            ),
            choice(spec["autonomy"], {"assist", "supervised", "autonomous", "unknown"}, "autonomy"),
            choice(spec["execution"], {"local", "temporal"}, "execution"),
            int(number(budget["maxSteps"], "maxSteps", 10000, True)),
            int(number(budget["maxSeconds"], "maxSeconds", 86400, True)),
            float(number(budget["maxCostUsd"], "maxCostUsd", 10000)),
            raw,
        )


def load(root: Path) -> Manifest:
    path = root / "agent.yaml"
    if not path.is_file() or path.stat().st_size > 128_000:
        raise ContractError("agent.yaml must exist and be <= 128 KB")
    try:
        return Manifest.parse(yaml.load(path.read_text(), Loader=UniqueLoader))
    except yaml.YAMLError as exc:
        raise ContractError(f"Invalid YAML: {exc}") from exc
