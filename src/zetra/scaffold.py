"""One runnable package template, shared by CLI and installed wheel."""

import re
import shutil
from importlib.resources import files
from pathlib import Path


def scaffold(target, name):
    if len(name) > 48 or not re.fullmatch(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*", name):
        raise ValueError("name must use lowercase words separated by hyphens or underscores")
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise ValueError("destination must be empty")
    source = Path(str(files("zetra") / "templates" / "agent"))
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", ".ruff_cache", ".pytest_cache", ".venv", "dist"
        ),
    )
    package = name.replace("-", "_")
    (target / "src" / "agent_package").rename(target / "src" / package)
    for p in target.rglob("*"):
        if p.is_file():
            p.write_text(
                p.read_text()
                .replace("agent_package", package)
                .replace("agent-package", name.replace("_", "-"))
            )
    return {"status": "CREATED", "path": str(target), "name": name}
