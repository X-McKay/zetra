"""Check bundled provenance and call the canonical, offline project renderer."""

import json
import tomllib
from importlib.resources import files
from pathlib import Path

from .build import sdk_dependency
from .templates.render import render


def scaffold(target, name):
    source = Path(str(files("zetra") / "templates" / "agent"))
    provenance = json.loads(source.parent.joinpath("provenance.json").read_text(encoding="utf-8"))
    qualified = provenance.get("qualified_sdk_revision")
    pinned = sdk_dependency(tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8")))[
        1
    ]
    if not qualified or qualified != pinned:
        raise ValueError(
            "this execution revision has no qualified template; use the released SDK CLI or canonical zetra-template renderer"
        )
    render(Path(target), name, source=source)
    return {"status": "CREATED", "path": str(target), "name": name}
