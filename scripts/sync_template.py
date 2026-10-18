"""Import or verify the canonical template from a local zetra-template checkout."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("checkout", type=Path)
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
source = args.checkout.resolve() / "template"
target = Path(__file__).resolve().parents[1] / "src/zetra/templates/agent"


def content(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and not any(
            part in {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "dist"}
            for part in p.relative_to(root).parts
        )
    }


expected = content(source)
if not expected or "uv.lock" not in expected:
    raise SystemExit("canonical template and lockfile required")
revision = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=args.checkout, text=True
).strip()
if subprocess.check_output(
    ["git", "status", "--porcelain", "--", "template"], cwd=args.checkout, text=True
).strip():
    raise SystemExit("commit canonical template changes before bundling")
identity = hashlib.sha256(
    json.dumps(
        {k: hashlib.sha256(v).hexdigest() for k, v in expected.items()}, sort_keys=True
    ).encode()
).hexdigest()
provenance = {
    "repository": "https://github.com/X-McKay/zetra-template",
    "revision": revision,
    "tree_sha256": identity,
}
if args.check:
    if content(target) != expected:
        raise SystemExit("bundled template differs from canonical tree")
    if json.loads((target.parent / "provenance.json").read_text()) != provenance:
        raise SystemExit("bundled template provenance differs from canonical checkout")
else:
    shutil.rmtree(target)
    for name, value in expected.items():
        output = target / name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(value)
    (target.parent / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
print("Canonical template matches SDK bundle and provenance")
