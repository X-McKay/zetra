"""Run developer-owned deterministic scenarios and bind candidate evidence to source."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__
from .analysis import risk, scan
from .manifest import ContractError, Manifest, mapping


def fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    # All material package contents, including prompts, binary assets and lock
    # files. Generated evidence belongs outside this root or under .zetra/.
    # deploy/ is generated output, not authoritative platform policy input.
    excluded = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".zetra",
        "deploy",
        "dist",
        "build",
        "node_modules",
    }
    paths = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if (
            any(part in excluded for part in relative.parts)
            or path.suffix == ".pyc"
            or any(part.endswith(".egg-info") for part in relative.parts)
        ):
            continue
        if path.is_symlink():
            raise ContractError("Agent package must not contain symlinks")
        if path.is_file():
            if path.stat().st_size > 20_000_000:
                raise ContractError("Individual package files must be <= 20 MB for local evidence")
            paths.append(path)
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ContractError("Agent source must not contain symlinks outside the package")
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def evaluator_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode() + path.read_bytes())
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size > 2_000_000:
        raise ContractError(f"JSON artifact missing or > 2 MB: {path}")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(),
            object_pairs_hook=unique,
            parse_constant=lambda s: (_ for _ in ()).throw(
                ContractError(f"Nonfinite JSON value: {s}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise ContractError(f"Invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ContractError("Expected JSON object")
    return value


def evaluate(root: Path, manifest: Manifest) -> dict:
    findings = scan(root, manifest)
    if findings:
        raise ContractError(f"Static check failed: {findings}")
    plan = read_json(root / "evals" / "scenarios.json")
    mapping(plan, {"scenarios"}, "scenario plan")
    if not isinstance(plan["scenarios"], dict):
        raise ContractError("scenarios must map stable scenario IDs to unittest test IDs")
    required = risk(manifest)["requiredScenarios"]
    missing = set(required) - set(plan["scenarios"])
    if missing:
        raise ContractError(f"Uncovered scenarios: {sorted(missing)}")
    source_hash = fingerprint(root)
    evaluator_hash = evaluator_digest()
    results = []
    for scenario in required:
        test_id = plan["scenarios"][scenario]
        if not isinstance(test_id, str) or not re.fullmatch(
            r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*){2,}", test_id
        ):
            raise ContractError(f"{scenario}: expected explicit module.Class.test_method ID")
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(root / "src"), str(root / "tests"), str(Path(__file__).parent.parent)]
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", test_id, "-v"],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
            )
            passed = (
                proc.returncode == 0
                and bool(re.search(r"Ran [1-9]\d* tests?", proc.stdout))
                and "skipped=" not in proc.stdout
                and "expected failures=" not in proc.stdout
            )
            results.append(
                {
                    "scenario": scenario,
                    "testId": test_id,
                    "status": "passed" if passed else "failed",
                    "exitCode": proc.returncode,
                    "output": proc.stdout[-12000:],
                }
            )
        except subprocess.TimeoutExpired:
            results.append(
                {
                    "scenario": scenario,
                    "testId": test_id,
                    "status": "failed",
                    "exitCode": None,
                    "output": "Scenario timed out after 60 seconds",
                }
            )
    if source_hash != fingerprint(root):
        raise ContractError("Agent source changed while scenarios executed")
    if evaluator_hash != evaluator_digest():
        raise ContractError("Toolkit evaluator changed while scenarios executed")
    return {
        "apiVersion": "zetra.dev/evidence-v1alpha1",
        "agent": manifest.name,
        "toolkitVersion": __version__,
        "sourceHash": source_hash,
        "manifestHash": hashlib.sha256((root / "agent.yaml").read_bytes()).hexdigest(),
        "evaluatorHash": evaluator_hash,
        "createdAt": datetime.now(UTC).isoformat(),
        "status": "passed" if all(r["status"] == "passed" for r in results) else "failed",
        "scenarios": results,
        "trust": "local-unsigned-candidate",
        "limitations": "Test assertions need independent review; no cluster, isolation or live adapter qualification is implied.",
    }


def validate_evidence(root: Path, manifest: Manifest, evidence: dict) -> None:
    if (
        evidence.get("apiVersion") != "zetra.dev/evidence-v1alpha1"
        or evidence.get("agent") != manifest.name
    ):
        raise ContractError("Evidence schema or agent identity mismatch")
    if (
        evidence.get("sourceHash") != fingerprint(root)
        or evidence.get("manifestHash")
        != hashlib.sha256((root / "agent.yaml").read_bytes()).hexdigest()
    ):
        raise ContractError("Evidence is stale: source or manifest hash mismatch")
    if (
        evidence.get("evaluatorHash") != evaluator_digest()
        or evidence.get("toolkitVersion") != __version__
    ):
        raise ContractError("Evidence evaluator/toolkit version mismatch")
    try:
        created = datetime.fromisoformat(evidence["createdAt"])
        age = datetime.now(UTC) - created
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("Invalid evidence timestamp") from exc
    if not timedelta(0) <= age <= timedelta(hours=24):
        raise ContractError("Evidence must be no older than 24 hours and not future-dated")
    scenarios = evidence.get("scenarios")
    if not isinstance(scenarios, list) or any(not isinstance(s, dict) for s in scenarios):
        raise ContractError("Evidence scenarios missing")
    ids = [s.get("scenario") for s in scenarios]
    if any(not isinstance(x, str) for x in ids) or len(ids) != len(set(ids)):
        raise ContractError("Evidence scenario IDs must be unique strings")
    by_id = {s["scenario"]: s for s in scenarios}
    required = risk(manifest)["requiredScenarios"]
    if evidence.get("status") != "passed" or any(
        by_id.get(s, {}).get("status") != "passed" or by_id.get(s, {}).get("exitCode") != 0
        for s in required
    ):
        raise ContractError("Release gate failed: required scenarios missing or not passed")
    if evidence.get("trust") != "local-unsigned-candidate":
        raise ContractError("Unsupported evidence trust model")
