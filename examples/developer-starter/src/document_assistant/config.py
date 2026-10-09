"""Load curator-owned TOML with explicit fields and confined fixture paths."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .models import ContractError, document_id


@dataclass(frozen=True)
class Config:
    name: str
    instruction_file: Path
    document_root: Path
    allowed_document_ids: frozenset[str]
    max_documents: int


def confined(root: Path, value: object) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or Path(value).is_absolute()
        or ".." in Path(value).parts
    ):
        raise ContractError("Configured paths must stay within the starter directory")
    path = root / value
    if any(
        parent.is_symlink() for parent in (path, *path.parents)
    ) or not path.resolve().is_relative_to(root.resolve()):
        raise ContractError("Configured fixture paths must not use symlinks or escape the starter")
    return path


def load_config(root: Path) -> Config:
    try:
        raw = tomllib.loads((root / "application.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ContractError("application.toml could not be loaded") from exc
    if set(raw) != {"application", "fixture"} or raw["fixture"] != {"kind": "offline-extractive"}:
        raise ContractError("Only the declared offline-extractive fixture is supported")
    app = raw["application"]
    required = {
        "name",
        "instruction_file",
        "document_root",
        "allowed_document_ids",
        "max_documents",
    }
    if not isinstance(app, dict) or set(app) != required:
        raise ContractError("Application config fields do not match the contract")
    if not isinstance(app["name"], str) or not app["name"].strip():
        raise ContractError("Application name must be nonempty")
    allowed = app["allowed_document_ids"]
    if not isinstance(allowed, list) or not allowed or len(allowed) > 8:
        raise ContractError("Approved IDs must be a list of 1..8 document IDs")
    identifiers = frozenset(document_id(item) for item in allowed)
    if len(identifiers) != len(allowed):
        raise ContractError("Approved document IDs must be unique")
    if type(app["max_documents"]) is not int or not 1 <= app["max_documents"] <= 8:
        raise ContractError("max_documents must be an integer 1..8")
    return Config(
        app["name"],
        confined(root, app["instruction_file"]),
        confined(root, app["document_root"]),
        identifiers,
        app["max_documents"],
    )
