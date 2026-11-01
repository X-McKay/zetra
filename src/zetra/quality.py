"""Explain local evidence requirements and execute a fail-closed development gate."""

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .eval_cases import load_cases
from .manifest import digest, load_agent, project_file, relative_path, resource_parts, source_digest

KINDS = {
    "outcome",
    "trajectory",
    "performance",
    "contract",
    "security",
    "recovery",
    "activation",
    "nonactivation",
}
TEST_KINDS = {"contract", "security", "recovery", "activation", "nonactivation"}
SUITABLE = {
    "outcome": {
        "text_contains",
        "text_not_contains",
        "text_equals",
        "file_contains",
        "file_not_contains",
        "file_exists",
        "file_equals",
        "file_absent",
    },
    "trajectory": {"tool_called", "tool_forbidden", "tool_before", "tool_result"},
    "performance": {
        "max_steps",
        "max_duration_ms",
        "max_input_tokens",
        "max_output_tokens",
        "max_total_tokens",
    },
}
NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,127}$")


def _name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError("invalid bounded identifier")


def _selector(root, value):
    if not isinstance(value, str) or len(value) > 1_024:
        raise ValueError("invalid pytest selector")
    parts = value.split("::")
    path = relative_path(parts[0])
    if (
        len(parts) < 2
        or path.parts[0] != "tests"
        or path.suffix != ".py"
        or any(not re.fullmatch(r"[A-Za-z_]\w*(?:\[[^\]\r\n]+\])?", part) for part in parts[1:])
    ):
        raise ValueError("expected tests/*.py::node selector")
    project_file(root, parts[0])


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate quality contract key")
        result[key] = value
    return result


def _load(root, agent):
    spec = load_agent(root, agent)
    path = project_file(root, f"evals/{spec.name}/quality.json")
    if path.stat().st_size > 1_000_000:
        raise ValueError("quality contract too large")
    raw = path.read_bytes()
    contract = json.loads(raw, object_pairs_hook=_unique)
    fields = {"schema_version", "agent", "tools", "skills", "live_model_required", "requirements"}
    if type(contract) is dict and contract.get("schema_version") == 2:
        fields.add("experiments")
    if (
        type(contract) is not dict
        or set(contract) != fields
        or type(contract["schema_version"]) is not int
        or contract["schema_version"] not in (1, 2)
        or contract["agent"] != spec.name
        or type(contract["live_model_required"]) is not bool
    ):
        raise ValueError("invalid quality contract fields, version or agent")
    if contract["schema_version"] == 2:
        from .eval_experiments import load_experiment

        names = contract["experiments"]
        if (
            type(names) is not list
            or not 1 <= len(names) <= 16
            or any(not isinstance(name, str) for name in names)
            or len(set(names)) != len(names)
        ):
            raise ValueError("expected unique nonempty experiment names")
        for name in names:
            load_experiment(root, spec.name, name)
    for field in ("tools", "skills"):
        entries = contract[field]
        if type(entries) is not dict or len(entries) > 100:
            raise ValueError(f"invalid {field} inventory")
        for name, value in entries.items():
            _name(name)
            if field == "tools":
                if not isinstance(value, str) or value not in {
                    "workspace_read",
                    "workspace_write",
                    "remote_read",
                }:
                    raise ValueError("invalid tool effect")
            else:
                if not isinstance(value, str):
                    raise ValueError("invalid skill resource")
                package, resource = resource_parts(value)
                project_file(root, (Path("src", *package.split(".")) / resource).as_posix())
    cases, dataset = load_cases(root, spec.name)
    by_id = {case["id"]: case for case in cases}
    requirements = contract["requirements"]
    if type(requirements) is not list or not 1 <= len(requirements) <= 1_000:
        raise ValueError("expected nonempty bounded quality requirements")
    seen = set()
    components = (
        {"agent"}
        | {f"tool:{name}" for name in contract["tools"]}
        | {f"skill:{name}" for name in contract["skills"]}
    )
    for requirement in requirements:
        if type(requirement) is not dict:
            raise ValueError("invalid requirement object")
        evidence = "cases" if "cases" in requirement else "tests"
        if set(requirement) != {"id", "component", "kind", evidence}:
            raise ValueError("requirement requires exactly one cases/tests mapping")
        _name(requirement["id"])
        if requirement["id"] in seen:
            raise ValueError("duplicate quality requirement ID")
        seen.add(requirement["id"])
        if (
            not isinstance(requirement["component"], str)
            or requirement["component"] not in components
        ):
            raise ValueError("unknown requirement component")
        kind = requirement["kind"]
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("unknown requirement kind")
        refs = requirement[evidence]
        if (
            type(refs) is not list
            or not 1 <= len(refs) <= 1_000
            or any(not isinstance(ref, str) for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            raise ValueError("expected unique nonempty evidence references")
        if evidence == "tests":
            for ref in refs:
                _selector(root, ref)
        else:
            if kind in TEST_KINDS:
                raise ValueError(f"{kind} requirements require executed tests")
            for ref in refs:
                if ref not in by_id:
                    raise ValueError(f"unknown case: {ref}")
                if not any(
                    assertion["type"] in SUITABLE[kind] for assertion in by_id[ref]["assertions"]
                ):
                    raise ValueError(f"case {ref} lacks suitable {kind} assertion")
    return spec, contract, raw, cases, dataset


def _missing(contract):
    required = {("agent", "outcome"), ("agent", "performance")}
    if contract["tools"]:
        required.add(("agent", "trajectory"))
    for tool in contract["tools"]:
        required.update((f"tool:{tool}", kind) for kind in ("contract", "security", "recovery"))
    for skill in contract["skills"]:
        required.update(
            (f"skill:{skill}", kind) for kind in ("contract", "activation", "nonactivation")
        )
    declared = {(row["component"], row["kind"]) for row in contract["requirements"]}
    return [
        f"missing requirement: {component}/{kind}"
        for component, kind in sorted(required - declared)
    ]


def explain_quality(root, *, agent=None):
    """Validate metadata without importing factories, test modules or graders."""
    try:
        spec, contract, raw, _, _ = _load(root, agent)
        missing = _missing(contract)
        return {
            "status": "FAILED" if missing else "PASSED",
            "agent": spec.name,
            "missing": missing,
            "requirements": contract["requirements"],
            "contract_sha256": hashlib.sha256(raw).hexdigest(),
            "tools": contract["tools"],
            "skills": contract["skills"],
            "experiments": _explain_experiments(root, spec, contract),
            "scope": "static local requirements; labels do not prove test semantics",
        }
    except (ValueError, OSError, TypeError, RecursionError) as exc:
        return {
            "status": "FAILED",
            "missing": [str(exc)],
            "requirements": [],
            "contract_sha256": None,
        }


def _explain_experiments(root, spec, contract):
    from .eval_experiments import load_experiment
    from .eval_statistics import wilson

    rows = []
    for name in contract.get("experiments", []):
        loaded = load_experiment(root, spec.name, name)
        plan = loaded.plan
        confidence = 1 - (1 - plan.confidence) / len(loaded.cases)
        lower = wilson(plan.trials, plan.trials, confidence)["lower"]
        invocations = len(loaded.cases) * plan.trials
        rows.append(
            {
                "name": name,
                "dataset": plan.dataset,
                "trials_per_case": plan.trials,
                "planned_invocations": invocations,
                "model_request_ceiling": invocations * plan.limits.model_calls_per_trial,
                "all_pass_lower_bound": lower,
                "target_lower_bound": plan.min_case_success_lower_bound,
                "sufficient_if_all_pass": lower >= plan.min_case_success_lower_bound,
                "purpose": "reliability" if plan.min_case_success_lower_bound else "smoke",
            }
        )
    return rows


def _inputs(root, spec, contract):
    names = {f"evals/{spec.name}/quality.json", f"evals/{spec.name}/cases.jsonl"}
    if contract.get("experiments"):
        from .eval_experiments import load_experiment

        for name in contract["experiments"]:
            loaded = load_experiment(root, spec.name, name)
            names.add(f"evals/{spec.name}/experiments/{name}.json")
            names.add(f"evals/{spec.name}/{loaded.plan.dataset}")
    names.update(path.relative_to(root).as_posix() for path in (root / "tests").rglob("*.py"))
    names.update(path.relative_to(root).as_posix() for path in root.glob("conftest.py"))
    names.update(
        name
        for name in ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg")
        if (root / name).exists()
    )
    return {
        "source_sha256": source_digest(root),
        "quality_inputs_sha256": digest(
            {
                name: hashlib.sha256(project_file(root, name).read_bytes()).hexdigest()
                for name in sorted(names)
            }
        ),
    }


def _run_pytest(root, selectors):
    with tempfile.TemporaryDirectory(prefix="zetra-quality-") as private:
        path = Path(private) / "pytest.json"
        sdk_src = str(Path(__file__).resolve().parents[1])
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(root / "src"), sdk_src)),
        }
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "zetra.testing.quality_plugin",
            "--zetra-report",
            str(path),
            "--rootdir",
            str(root),
            "--confcutdir",
            str(root),
            "-q",
            *selectors,
        ]
        try:
            process = subprocess.run(
                command,
                cwd=root,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )
            evidence = (
                json.loads(path.read_text()) if path.is_file() else {"collected": [], "reports": []}
            )
            evidence["returncode"] = process.returncode
            evidence["phase_counts"] = {
                outcome: sum(row.get("outcome") == outcome for row in evidence["reports"])
                for outcome in ("passed", "failed", "skipped")
            }
            return evidence
        except subprocess.TimeoutExpired:
            return {
                "collected": [],
                "reports": [],
                "returncode": -1,
                "error": "TimeoutExpired",
            }
        except (ValueError, OSError) as exc:
            return {"collected": [], "reports": [], "returncode": -1, "error": type(exc).__name__}


def _test_passed(selector, evidence):
    candidates = set(evidence.get("all_collected", evidence.get("collected", [])))
    candidates.update(evidence.get("deselected", []))
    nodes = [node for node in candidates if node == selector or node.startswith(selector + "[")]
    if not nodes or evidence.get("returncode") != 0:
        return False
    for node in nodes:
        reports = [row for row in evidence.get("reports", []) if row.get("nodeid") == node]
        if len(reports) != 3 or {row.get("phase") for row in reports} != {
            "setup",
            "call",
            "teardown",
        }:
            return False
        if any(row.get("outcome") != "passed" or row.get("wasxfail") for row in reports):
            return False
    return True


async def run_quality(
    root, *, agent=None, mode="fixture", model_config=None, mcp_fixtures=None, experiment=None
):
    """Run fresh mapped tests and evaluations; evidence is never production approval."""
    from .bindings import MCPFixtures, mcp_provenance
    from .evaluation import evaluate

    fixtures = MCPFixtures(mcp_fixtures) if mcp_fixtures is not None else None

    root = Path(root).resolve()
    explanation = explain_quality(root, agent=agent)
    report = {
        **explanation,
        "mode": mode,
        "trust": "local-unsigned",
        "scope": "local executed evidence; not production authorization",
        "pytest": None,
        "evaluation": None,
        "experiment": None,
        "mcp_binding": mcp_provenance(fixtures),
    }
    if explanation["status"] != "PASSED":
        return report
    spec, contract, raw, cases, dataset = _load(root, agent)
    missing = report["missing"]
    if experiment is not None:
        if mode != "local" or experiment not in contract.get("experiments", []):
            missing.append("live quality requires local mode and a declared experiment")
        if spec.model != "fixture":
            missing.append(
                "experiment quality requires a fixture baseline; use standalone eval for model-only projects"
            )
    elif contract["schema_version"] == 2 and mode == "local":
        missing.append("local quality requires an explicit declared experiment")
    if mode not in {"fixture", "local"}:
        missing.append("quality supports only explicit fixture or local model modes")
    if mode == "local" and model_config is None:
        missing.append("local quality requires explicit model configuration")
    if mode != "local" and model_config is not None:
        missing.append("model configuration is only valid for local quality")
    if mode == "fixture" and spec.model != "fixture":
        missing.append("fixture quality requires the fixture model")
    if mode == "local" and model_config is not None:
        from .local import LocalModelConfig

        try:
            model_config = LocalModelConfig.model_validate(model_config).model_dump()
        except (ValueError, TypeError) as exc:
            missing.append(f"invalid model configuration: {type(exc).__name__}")
    if contract["live_model_required"] and mode == "fixture":
        missing.append("live model evidence required; fixture cannot satisfy it")
    if missing:
        report["status"] = "FAILED"
        return report
    try:
        before = _inputs(root, spec, contract)
    except (ValueError, OSError) as exc:
        missing.append(f"quality inputs unavailable: {type(exc).__name__}")
        report["status"] = "FAILED"
        return report
    report.update(
        before,
        contract_sha256=hashlib.sha256(raw).hexdigest(),
        dataset_sha256=hashlib.sha256(dataset).hexdigest(),
    )
    selectors = sorted(
        {selector for row in contract["requirements"] for selector in row.get("tests", [])}
    )
    tests = (
        await asyncio.to_thread(_run_pytest, root, selectors)
        if selectors
        else {"collected": [], "reports": [], "returncode": 0}
    )
    report["pytest"] = tests
    if tests["returncode"] != 0:
        missing.append("pytest execution failed or evidence unavailable")
    try:
        options = {"mcp_fixtures": fixtures.config()} if fixtures is not None else {}
        evaluation = await evaluate(
            root,
            mode="fixture" if experiment else mode,
            agent=spec.name,
            model_config=None if experiment else model_config,
            **options,
        )
        # Manual live workflows upload this whole wrapper. Retain fixture gate
        # evidence but never include its raw tool arguments/results in that artifact.
        report["evaluation"] = (
            {
                **evaluation,
                "cases": [
                    {
                        **{
                            key: row[key]
                            for key in (
                                "id",
                                "passed",
                                "outcome",
                                "status",
                                "cleanup",
                                "error_type",
                                "diagnostic",
                                "tools",
                                "metrics",
                            )
                            if key in row
                        },
                        "assertions": [
                            {"type": check["type"], "passed": check["passed"]}
                            for check in row.get("assertions", [])
                        ],
                    }
                    for row in evaluation.get("cases", [])
                ],
            }
            if experiment is not None
            else evaluation
        )
        if fixtures is not None and evaluation.get("mcp_binding") != report["mcp_binding"]:
            missing.append("MCP fixture evidence does not match the requested binding")
    except Exception as exc:  # noqa: BLE001 - preserve execution errors as failed evidence
        evaluation = {"status": "FAILED", "cases": []}
        missing.append(f"evaluation error: {type(exc).__name__}")
    rows = evaluation.get("cases", [])
    planned = {case["id"] for case in cases}
    if len(rows) != len(cases) or {row.get("id") for row in rows} != planned:
        missing.append("incomplete evaluation case accounting")
    counts = evaluation.get("counts", {})
    expected_counts = {
        "planned": len(cases),
        "passed": sum(row.get("passed") is True for row in rows),
        "failed": sum(row.get("outcome") == "failed" for row in rows),
        "error": sum(row.get("outcome") == "error" for row in rows),
    }
    if (
        type(counts) is not dict
        or any(type(value) is not int or value < 0 for value in counts.values())
        or counts != expected_counts
        or sum(expected_counts[key] for key in ("passed", "failed", "error")) != len(cases)
        or any(
            row.get("outcome") not in {"passed", "failed", "error"}
            or (row.get("outcome") == "passed") != (row.get("passed") is True)
            for row in rows
        )
    ):
        missing.append("evaluation counts do not match complete case outcomes")
    if evaluation.get("status") != "PASSED" or any(row.get("passed") is not True for row in rows):
        missing.append("evaluation did not pass every planned case")
    for row in rows:
        if row.get("tools") != contract["tools"]:
            missing.append(f"case {row.get('id')}: admitted tool inventory differs from contract")
    requirement_rows = []
    by_id = {row.get("id"): row for row in rows}
    for requirement in contract["requirements"]:
        if "tests" in requirement:
            passed = all(_test_passed(selector, tests) for selector in requirement["tests"])
        else:
            passed = all(
                by_id.get(case_id, {}).get("passed") is True for case_id in requirement["cases"]
            )
        requirement_rows.append({**requirement, "passed": passed})
        if not passed:
            missing.append(f"unsatisfied requirement: {requirement['id']}")
    report["requirements"] = requirement_rows
    try:
        if before != _inputs(root, spec, contract):
            missing.append("source or quality inputs changed during execution")
    except (ValueError, OSError) as exc:
        missing.append(f"quality inputs changed or unavailable: {type(exc).__name__}")
    if experiment is not None and not missing:
        from .eval_experiments import run_experiment

        live = await run_experiment(
            root,
            agent=spec.name,
            experiment=experiment,
            model_config=model_config,
            mcp_fixtures=fixtures.config() if fixtures else None,
        )
        report["experiment"] = live
        if live["status"] != "PASSED":
            missing.append(f"live experiment {experiment}: {live['status']}")
        if any(
            trial["tools"] != contract["tools"]
            for case in live["cases"]
            for trial in case["trials"]
            if trial["outcome"] in {"passed", "failed"}
        ):
            missing.append("live experiment tool inventory differs from contract")
        try:
            if before != _inputs(root, spec, contract):
                missing.append("source or quality inputs changed during live execution")
        except (ValueError, OSError):
            missing.append("quality inputs unavailable after live execution")
    report["status"] = "FAILED" if missing else "PASSED"
    return report
