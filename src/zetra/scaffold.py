"""One runnable package template, shared by CLI and installed wheel."""

import re
import shutil
from importlib.resources import files
from pathlib import Path


def scaffold(target, name):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name):
        raise ValueError("package name must use lowercase letters, digits and underscores")
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise ValueError("destination must be empty")
    source = Path(str(files("zetra") / "templates" / "agent"))
    shutil.copytree(
        source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    (target / "src" / "agent_package").rename(target / "src" / name)
    for p in target.rglob("*"):
        if p.is_file():
            p.write_text(p.read_text().replace("agent_package", name))
    return {"status": "CREATED", "path": str(target), "name": name}
