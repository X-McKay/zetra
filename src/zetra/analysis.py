"""Best-effort static hints; never interpret these as a complete capability inventory."""

from __future__ import annotations

import ast
from pathlib import Path

from .manifest import Manifest

KNOWN = {
    "knowledge.read",
    "file.read",
    "file.write",
    "process.exec",
    "network.http",
    "model.invoke",
    "ticket.write",
    "tool.write",
    "mcp.read_status",
}
WRITES = {"file.write", "ticket.write", "tool.write"}
BASE_SCENARIOS = {
    "schema.contract",
    "input.untrusted",
    "capability.denied",
    "budget.enforced",
    "stop.enforced",
}


def risk(manifest: Manifest) -> dict:
    reasons = []
    tier = 0
    if manifest.data_classification in {"internal", "confidential"}:
        tier = 1
        reasons.append("Nonpublic data")
    if manifest.data_classification in {"restricted", "unknown"} or manifest.autonomy == "unknown":
        tier = 3
        reasons.append("Restricted or unknown data/autonomy requires risk review")
    if set(manifest.capabilities) & WRITES or manifest.autonomy == "autonomous":
        tier = max(tier, 2)
        reasons.append("Consequential write capability or autonomous execution")
    if set(manifest.capabilities) & WRITES and manifest.autonomy == "autonomous":
        tier = 3
        reasons.append("Autonomous consequential writes require highest-tier risk review")
    if "process.exec" in manifest.capabilities or set(manifest.capabilities) - KNOWN:
        tier = 3
        reasons.append("Process execution or unclassified capability")
    scenarios = set(BASE_SCENARIOS)
    if set(manifest.capabilities) & WRITES:
        scenarios.update({"approval.bound", "idempotency.retry"})
    if manifest.data_classification in {"confidential", "restricted", "unknown"}:
        scenarios.add("data.redacted")
    if manifest.autonomy == "autonomous":
        scenarios.add("trajectory.stop")
    if manifest.execution == "temporal":
        scenarios.add("workflow.replay")
    if "mcp.read_status" in manifest.capabilities:
        scenarios.add("gateway.authorization")
    return {
        "tier": f"T{tier}",
        "reasons": reasons or ["Declared read-only public-data assistive use"],
        "requiredScenarios": sorted(scenarios),
        "status": "candidate",
        "humanReviewRequired": tier >= 2,
        "limitations": "Declaration-based floor; business impact, jurisdiction and external evidence may only raise it.",
    }


def scan(root: Path, manifest: Manifest) -> list[dict]:
    findings = []
    for path in sorted((root / "src").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as exc:
            findings.append(
                {
                    "path": str(path.relative_to(root)),
                    "line": exc.lineno,
                    "severity": "error",
                    "message": "Python syntax error",
                }
            )
            continue
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update({x.asname or x.name.split(".")[0]: x.name for x in node.names})
            elif isinstance(node, ast.ImportFrom):
                aliases.update({x.asname or x.name: f"{node.module}.{x.name}" for x in node.names})

        def name(node, aliases=aliases):
            if isinstance(node, ast.Name):
                return aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return f"{name(node.value)}.{node.attr}"
            return ""

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = name(node.func)
            cap = None
            if called.startswith("subprocess.") or called in {
                "os.system",
                "os.execv",
                "os.execve",
                "os.popen",
            }:
                cap = "process.exec"
            elif called.startswith(("requests.", "httpx.", "urllib.request.", "socket.")):
                cap = "network.http"
            elif called == "open" or called.endswith(".open"):
                mode = (
                    node.args[1]
                    if len(node.args) > 1
                    else next(
                        (x.value for x in node.keywords if x.arg == "mode"), ast.Constant("r")
                    )
                )
                if not isinstance(mode, ast.Constant) or not isinstance(mode.value, str):
                    cap = "file.write"
                else:
                    cap = "file.write" if any(c in mode.value for c in "wa+") else "file.read"
            elif called.endswith((".write_text", ".write_bytes", ".unlink", ".mkdir", ".rename")):
                cap = "file.write"
            if called in {"eval", "exec", "__import__", "importlib.import_module"}:
                findings.append(
                    {
                        "path": str(path.relative_to(root)),
                        "line": node.lineno,
                        "severity": "error",
                        "message": f"Dynamic execution requires security review: {called}",
                    }
                )
            if cap and cap not in manifest.capabilities:
                findings.append(
                    {
                        "path": str(path.relative_to(root)),
                        "line": node.lineno,
                        "severity": "error",
                        "message": f"Undeclared capability hint: {cap} via {called}",
                    }
                )
    return findings
