"""Evidence files and the report tool most agents end with (spec 11.7).

Both write only inside the workspace. The report tool is declared `workspace_write`; a
tool that reads through `ctx.mcp` and records what came back stays `remote_read`, since
the evidence file is the record of that read. The report tool appends the exact links,
so a report can cite only what was actually observed:

    def create_agent(ctx) -> dict:
        evidence = Evidence(ctx.workspace)

        async def list_pods() -> dict:
            '''List the demonstration pods.'''
            result = await ctx.mcp("kubernetes", "pods_list_in_namespace", {"namespace": "demo"})
            return evidence.record("pods", {"tool": result["tool"], "text": result["text"]})

        return {"tools": [Tool(list_pods, "remote_read"), report_tool(ctx.workspace, evidence)]}
"""

import hashlib
import json
import pathlib
import re

from agent_runtime.tools import Tool

LABEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,80}")
FILE = re.compile(r"([a-z0-9][a-z0-9_-]{0,80})-([a-f0-9]{64})\.json")
REPORT = "report.md"


class EvidenceError(ValueError):
    """The evidence directory holds something `record` did not write."""


class EvidenceLimit(ValueError):
    """The evidence directory is full; nothing was written."""


class Evidence:
    """`evidence/<label>-<sha256 of the file>.json` under the workspace, bounded in count
    and size. A file's name is the hash of its bytes, so a link in a report identifies
    exactly what was observed and a changed file no longer verifies."""

    def __init__(
        self, workspace: pathlib.Path, *, limit: int = 32, max_bytes: int = 24_000
    ) -> None:
        self.workspace = pathlib.Path(workspace)
        self.directory = self.workspace / "evidence"
        self.limit = limit
        self.max_bytes = max_bytes

    def files(self) -> list[pathlib.Path]:
        """Every evidence file, sorted by name, each verified against its own name."""
        if self.directory.is_symlink():
            raise EvidenceError("the evidence directory is a symbolic link")
        if not self.directory.is_dir():
            return []
        files = sorted(self.directory.iterdir())
        if len(files) > self.limit:
            raise EvidenceError("more evidence files than the limit")
        for path in files:
            name = FILE.fullmatch(path.name)
            if name is None or path.is_symlink() or not path.is_file():
                raise EvidenceError("an evidence file was not written by record")
            if path.stat().st_size > self.max_bytes:
                raise EvidenceError("an evidence file exceeds the size limit")
            if hashlib.sha256(path.read_bytes()).hexdigest() != name.group(2):
                raise EvidenceError("an evidence file does not match its name")
        return files

    def links(self) -> list[str]:
        """Workspace-relative paths of the evidence files, as a report cites them."""
        return [path.relative_to(self.workspace).as_posix() for path in self.files()]

    def record(self, label: str, data: dict) -> dict:
        """Write `data` as one evidence file and return it with its `evidence` path, which
        is what a tool hands back to the model. Recording the same data again is the same
        file."""
        if not isinstance(label, str) or not LABEL.fullmatch(label):
            raise ValueError("evidence label must be lowercase letters, digits, - or _")
        if not isinstance(data, dict):
            raise TypeError("evidence must be a JSON object")
        encoded = json.dumps(data, sort_keys=True, indent=2).encode()
        if len(encoded) > self.max_bytes:
            raise ValueError(f"evidence record exceeds {self.max_bytes} bytes")
        name = f"{label}-{hashlib.sha256(encoded).hexdigest()}.json"
        path = self.directory / name
        existing = self.files()
        if path not in existing:
            if len(existing) >= self.limit:
                raise EvidenceLimit(f"at most {self.limit} evidence files")
            self.directory.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as output:
                output.write(encoded)
        return {**data, "evidence": "evidence/" + name}


def report_tool(
    workspace: pathlib.Path,
    evidence: Evidence | None = None,
    *,
    max_words: int = 150,
    max_bytes: int = 8_000,
    description: str | None = None,
) -> Tool:
    """The `write_report` tool: a bounded narrative written to `report.md` in the workspace.

    With `evidence`, the report must follow at least one recorded observation, the
    narrative may not name evidence paths itself, and the exact links are appended, so the
    model cannot cite a file that was never written. Refusals are returned to the model as
    `{"error": ...}` so it can correct itself; `description` replaces the tool description
    the model sees.
    """
    workspace = pathlib.Path(workspace)
    noun = "narrative" if evidence is not None else "report"

    async def write_report(content: str) -> dict:
        if not isinstance(content, str):
            return {"error": f"Write the {noun} as text."}
        if evidence is not None and "evidence/" in content:
            return {"error": "Omit evidence paths from the narrative; exact links are appended."}
        if not content.strip() or len(content.split()) > max_words:
            return {"error": f"Write a nonempty {noun} of at most {max_words} words."}
        report = content.rstrip() + "\n"
        if evidence is not None:
            links = evidence.links()
            if not links:
                return {"error": "Gather evidence before writing the report."}
            report += "\n## Evidence\n\n" + "\n".join(f"- [{p}]({p})" for p in links) + "\n"
        if len(report.encode()) > max_bytes:
            return {"error": f"Keep the report below {max_bytes} UTF-8 bytes."}
        path = workspace / REPORT
        if path.is_symlink():  # a restored workspace may hold one; never write through it
            path.unlink()
        path.write_text(report, encoding="utf-8")
        return {"path": REPORT}

    write_report.__doc__ = description or (
        f"Write narrative under {max_words} words; exact evidence links are appended automatically."
        if evidence is not None
        else f"Write the report; at most {max_words} words of plain text."
    )
    return Tool(write_report, "workspace_write")
