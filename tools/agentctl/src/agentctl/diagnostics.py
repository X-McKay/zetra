from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from agentctl.ui import diagnostics_table


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    path: str
    severity: str = "error"

    def render(self) -> str:
        return f"{self.severity.upper()} {self.code} {self.path}: {self.message}"


def emit_diagnostics(
    diagnostics: Iterable[Diagnostic], *, output_format: str, heading: str
) -> int:
    items = list(diagnostics)
    if output_format == "json":
        print(
            json.dumps(
                {
                    "ok": not any(item.severity == "error" for item in items),
                    "diagnostics": [asdict(item) for item in items],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        diagnostics_table(items, heading_text=heading)
    return int(any(item.severity == "error" for item in items))


def relative_display(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)
