"""Check or explicitly synchronize the repository's managed developer skill pack."""

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path


def safe_path(root: Path, relative: str) -> Path:
    path = root
    for part in relative.split("/"):
        if not part or part in {".", ".."} or "\\" in part or ":" in part:
            raise ValueError(f"unsafe inventory path: {relative}")
        path = path / part
        if path.is_symlink():
            raise ValueError(f"symlink is not allowed in managed paths: {path}")
    return path


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def replace(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def synchronize(root: Path, *, check: bool) -> None:
    root = root.resolve()
    manifest = safe_path(root, "developer-skills.json")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or not isinstance(data.get("pack_version"), str)
        or not re.fullmatch(r"\d+\.\d+\.\d+", data["pack_version"])
        or not isinstance(data.get("files"), dict)
        or not data["files"]
    ):
        raise ValueError("invalid developer skill inventory")
    files = data["files"]
    for relative, recorded in files.items():
        parts = relative.split("/")
        if (
            len(parts) < 2
            or not re.fullmatch(r"zetra-[a-z0-9]+(?:-[a-z0-9]+)*", parts[0])
            or not isinstance(recorded, str)
            or not re.fullmatch(r"[0-9a-f]{64}", recorded)
        ):
            raise ValueError(f"invalid inventory entry: {relative}")
        safe_path(root, relative)
    names = {relative.split("/")[0] for relative in files}
    if any(f"{name}/SKILL.md" not in files for name in names):
        raise ValueError("every managed skill requires an inventoried SKILL.md")

    # Inspect both trees and every conflict before any writes. Unknown skill names
    # are project-owned; extra files inside a managed skill must be reconciled.
    for prefix in (".agents/skills", ".claude/skills"):
        safe_path(root, prefix)
        for name in sorted(names):
            directory = safe_path(root, f"{prefix}/{name}")
            if directory.exists() and not directory.is_dir():
                raise ValueError(f"expected a skill directory: {directory}")
            for path in sorted(directory.rglob("*")):
                relative = path.relative_to(root / prefix).as_posix()
                safe_path(root, f"{prefix}/{relative}")
                if not path.is_dir() and relative not in files:
                    raise ValueError(f"unmanaged file inside managed skill: {path}")

    changes = []
    hashes = {}
    for relative, recorded in sorted(files.items()):
        source = safe_path(root, f".agents/skills/{relative}")
        target = safe_path(root, f".claude/skills/{relative}")
        content = source.read_bytes()
        hashes[relative] = digest(content)
        existing = target.read_bytes() if target.exists() else None
        if check:
            if hashes[relative] != recorded or existing != content:
                raise ValueError(f"developer skill drift: {relative}; review before --sync")
        elif existing is not None and existing != content and digest(existing) != recorded:
            raise ValueError(f"local mirror conflict: {relative}; preserve and reconcile edits")
        if existing != content:
            changes.append((target, content))
    if not check:
        for target, content in changes:
            replace(target, content)
        if hashes != files:
            data["files"] = hashes
            replace(manifest, (json.dumps(data, indent=2) + "\n").encode())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--sync", action="store_true")
    args = parser.parse_args()
    try:
        synchronize(args.root, check=args.check)
    except (OSError, ValueError) as exc:
        print(f"developer-skills: {exc}", file=sys.stderr)
        return 1
    print("Developer skill pack matches its inventory and both assistant locations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
