"""Complete, explicitly scoped evaluation of local or durable agent executions."""

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .eval_cases import grade, load_cases
from .manifest import digest, load_agent, project_file, source_digest


async def fixture_run(root, message, *, agent=None, mcp_fixtures=None):
    from .local import local_run

    return await local_run(root, message, agent=agent, mcp_fixtures=mcp_fixtures)


async def _fixture_run_in_process(root, message, *, agent=None):
    from .local import _run_in_process

    return await _run_in_process(root, message, agent=agent)


async def evaluate_case(
    root,
    case,
    *,
    mode,
    agent,
    model_config=None,
    mcp_fixtures=None,
    release=None,
    client=None,
    timeout=60,
    model_call_limit=None,
):
    """Shared single-case execution and grading; caller owns trial accounting."""
    from .local import local_run

    result = {}
    assertions = []
    try:
        if mode == "fixture":
            options = {"mcp_fixtures": mcp_fixtures} if mcp_fixtures is not None else {}
            result = await fixture_run(root, case["input"], agent=agent, **options)
        elif mode == "local":
            options = {"model_call_limit": model_call_limit} if model_call_limit is not None else {}
            result = await local_run(
                root,
                case["input"],
                agent=agent,
                model_config=model_config,
                mcp_fixtures=mcp_fixtures,
                timeout=timeout,
                **options,
            )
        else:
            handle = await client.submit(
                release, session="eval-" + uuid.uuid4().hex, message=case["input"]
            )
            result = await client.wait(handle)
            if result.get("status") == "SUCCEEDED" and result.get("cleanup") == "DELETED":
                files = await client.read_files(handle)
                result["files"] = list(files)
                result["file_contents"] = {}
                for name, content in files.items():
                    if len(content) <= 65536:
                        try:
                            result["file_contents"][name] = content.decode("utf-8")
                        except UnicodeError:
                            pass
        successful = result.get("status") == "SUCCEEDED" and (
            mode != "sandbox" or result.get("cleanup") == "DELETED"
        )
        assertions = grade(case, result)
        outcome = (
            "error"
            if not successful
            else ("passed" if all(item["passed"] for item in assertions) else "failed")
        )
    except Exception as exc:  # noqa: BLE001 -- never serialize provider secrets
        result = {"status": "ERROR", "error_type": type(exc).__name__}
        outcome = "error"
    return {
        "id": case["id"],
        "passed": outcome == "passed",
        "outcome": outcome,
        "status": result.get("status"),
        "cleanup": result.get("cleanup"),
        "error_type": result.get("error_type"),
        **({"diagnostic": result["diagnostic"]} if result.get("diagnostic") is not None else {}),
        "assertions": assertions,
        "tools": result.get("tools", {}),
        "metrics": result.get("metrics", {}),
        "trajectory": result.get("trajectory", []),
    }


async def evaluate(
    root, *, mode, release=None, client=None, agent=None, model_config=None, mcp_fixtures=None
):
    from .bindings import MCPFixtures, mcp_provenance
    from .local import LocalModelConfig

    root = Path(root)
    spec = load_agent(root, agent)
    source = source_digest(root)
    if mode == "sandbox" and mcp_fixtures is not None:
        raise ValueError("MCP fixture overrides cannot be used in sandbox evaluation")
    fixtures = MCPFixtures(mcp_fixtures) if mcp_fixtures is not None else None
    fixture_config = fixtures.config() if fixtures else None
    if mode not in {"fixture", "local", "sandbox"}:
        raise ValueError("unknown evaluation mode")
    if mode == "local" and model_config is None:
        raise ValueError("local evaluation requires explicit model configuration")
    if mode != "local" and model_config is not None:
        raise ValueError("model configuration is only valid for local evaluation")
    config = LocalModelConfig.model_validate(model_config) if model_config is not None else None
    if mode == "fixture" and spec.model != "fixture":
        raise ValueError("fixture mode requires the fixture model; it cannot issue inference")
    if mode == "sandbox" and (
        release is None
        or client is None
        or release.agent != spec
        or release.source_sha256 != source
    ):
        raise ValueError("sandbox evaluation requires matching release and source")
    cases, dataset = load_cases(root, spec.name)
    rows = []
    for case in cases:
        rows.append(
            await evaluate_case(
                root,
                case,
                mode=mode,
                agent=spec.name,
                model_config=config.model_dump() if config else None,
                mcp_fixtures=fixture_config,
                release=release,
                client=client,
            )
        )
    integrity_errors = []
    try:
        if dataset != project_file(root, f"evals/{spec.name}/cases.jsonl").read_bytes():
            integrity_errors.append("dataset_changed")
        if source != source_digest(root):
            integrity_errors.append("source_changed")
    except (ValueError, OSError):
        integrity_errors.append("inputs_unreadable")
    counts = {
        "planned": len(rows),
        **{name: sum(r["outcome"] == name for r in rows) for name in ("passed", "failed", "error")},
    }
    effective_model = config.model_dump() if config else {"model": spec.model}
    return {
        "schema_version": 2,
        "mode": mode,
        "agent": spec.name,
        "agent_sha256": digest(spec.model_dump()),
        "status": "PASSED"
        if counts["passed"] == counts["planned"] and not integrity_errors
        else "FAILED",
        "source_sha256": source,
        "release": release.identity if release else None,
        "created_at": datetime.now(UTC).isoformat(),
        "trust": "local-unsigned",
        "sdk": "0.1.0",
        "dataset_sha256": hashlib.sha256(dataset).hexdigest(),
        "model": spec.model,
        "model_config": effective_model,
        "model_config_sha256": digest(effective_model),
        "mcp_binding": mcp_provenance(fixtures),
        "policy": spec.policy,
        "profile": spec.profile,
        "cases": rows,
        "counts": counts,
        "integrity_errors": integrity_errors,
        "scope": {
            "fixture": "fixture behavior",
            "local": "trusted host model behavior",
            "sandbox": "durable sandbox cases",
        }[mode],
    }
