"""Render the canonical SDK-compatible tree using only the standard library."""

import argparse
import keyword
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

# Import names differing from locked distribution names (including namespaces).
# Keep this list aligned with template/uv.lock; no dependency imports are executed.
DEPENDENCY_IMPORT_ALIASES = {
    "google",
    "grpc",
    "dotenv",
    "multipart",
    "yaml",
    "rpds",
    "attr",
    "opentelemetry",
    "importlib_metadata",
    "jinja2",
    "huggingface_hub",
    "pygments",
    "typing_extensions",
    "_cffi_backend",
    "nexusrpc",
    "py",
    "tiktoken_ext",
    "hf_xet",
}


def render(destination: Path, name: str, *, source: Path | None = None) -> None:
    if len(name) > 48 or not re.fullmatch(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*", name):
        raise ValueError(
            "name must be at most 48 characters: lowercase words separated by hyphens or underscores"
        )
    source = (
        Path(source) if source is not None else Path(__file__).resolve().parents[1] / "template"
    )
    destination = Path(destination)
    package = name.replace("-", "_")
    distribution = name.replace("_", "-")
    reserved = set(sys.stdlib_module_names) | set(keyword.kwlist) | DEPENDENCY_IMPORT_ALIASES
    lock_path = source / "uv.lock"
    if lock_path.is_file():
        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        reserved.update(
            p["name"].replace("-", "_") for p in lock["package"] if p["name"] != "agent-package"
        )
    if package in reserved:
        raise ValueError(f"name is reserved by Python or a dependency: {name}")
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination already exists")
    if not source.is_dir():
        raise ValueError("template source is missing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        for item in sorted(source.rglob("*")):
            if not item.is_file() or any(
                p in {"__pycache__", ".pytest_cache", ".venv", ".ruff_cache", "dist"}
                for p in item.relative_to(source).parts
            ):
                continue
            relative = str(item.relative_to(source)).replace("agent_package", package)
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            text = (
                item.read_text(encoding="utf-8")
                .replace("agent_package", package)
                .replace("agent-package", distribution)
            )
            target.write_text(text, encoding="utf-8")
        # Reserve the destination exclusively so a concurrent creator is never replaced.
        destination.mkdir()
        try:
            for item in staging.iterdir():
                item.rename(destination / item.name)
        except BaseException:
            shutil.rmtree(destination)
            raise
    finally:
        shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    try:
        render(args.path, args.name)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
