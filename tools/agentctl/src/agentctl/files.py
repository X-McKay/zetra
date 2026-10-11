from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import yaml


class DataFileError(ValueError):
    pass


def load_data(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DataFileError(f"cannot read {path}: {exc}") from exc
    try:
        value = (
            json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
        )
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise DataFileError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DataFileError(f"{path} must contain a mapping at the top level")
    return value


def dump_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)
