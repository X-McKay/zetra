"""Validate repository documentation links without network access."""

import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
paths = [
    root / "README.md",
    root / "CONTRIBUTING.md",
    root / "SECURITY.md",
    root / "AGENTS.md",
    *sorted((root / "docs").glob("*.md")),
]
for path in paths:
    for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
        if "://" in target or target.startswith("#"):
            continue
        assert (path.parent / target.split("#")[0]).exists(), (path, target)
print(f"{len(paths)} documentation files: links valid")
