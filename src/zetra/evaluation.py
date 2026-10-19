"""Explicit fixture execution and separately labelled sandbox evaluation."""

import hashlib
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .manifest import digest, load_agent, source_digest


async def fixture_run(root, message, *, agent=None):
    from ._runtime.process import run_child

    return await run_child(
        "zetra.testing.fixture",
        {"root": str(Path(root).resolve()), "message": message, "agent": agent},
        timeout=60,
    )


async def _fixture_run_in_process(root, message, *, agent=None):
    # This mode explicitly executes trusted developer code on the host.
    from ._runtime.artifacts import Receipt
    from ._runtime.definition import Definition
    from ._runtime.runtime import Runtime

    spec = load_agent(root, agent)
    if spec.model != "fixture":
        raise ValueError("fixture mode requires the fixture model; it cannot issue inference")
    sys.path.insert(0, str(Path(root).resolve() / "src"))
    try:
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(
                temp,
                session="fixture",
                manifest=source_digest(root),
                key=b"f" * 32,
                message=message,
                definition=Definition(spec.model_dump(), Path(temp) / "workspace"),
            )
            position = runtime.tape.empty(runtime.workspace.capture())
            try:
                for _ in range(65):
                    command = {
                        "binding": {"session": "fixture"},
                        "position": position.model_dump(),
                        "pending": None,
                    }
                    advanced = await runtime.handle(runtime.authority.sign("advance", command))
                    if advanced["status"] == "done":
                        return {
                            "status": "SUCCEEDED",
                            "result": advanced["result"],
                            "files": [
                                p.relative_to(runtime.workdir).as_posix()
                                for p in runtime.workdir.rglob("*")
                                if p.is_file()
                            ],
                        }
                    executed = await runtime.handle(
                        runtime.authority.sign(
                            "execute",
                            {
                                **command,
                                "operation": advanced["operation"],
                                "attempt_id": str(position.cursor),
                            },
                        )
                    )
                    position = Receipt.model_validate(executed["receipt"]).position
                raise ValueError("fixture boundary limit")
            finally:
                await runtime.close()
    finally:
        sys.path.pop(0)


async def evaluate(root, *, mode, release=None, client=None, agent=None):
    import uuid

    root = Path(root)
    spec = load_agent(root, agent)
    source = source_digest(root)
    if mode == "sandbox" and (
        release is None
        or client is None
        or release.agent != spec
        or release.source_sha256 != source
    ):
        raise ValueError("sandbox evaluation requires matching release and source")
    rows = []
    seen = set()
    dataset_path = root / "evals" / spec.name / "cases.jsonl"
    dataset = dataset_path.read_bytes()
    for line in dataset.decode().splitlines():
        case = json.loads(line)
        if set(case) != {"id", "input", "contains", "files"} or case["id"] in seen:
            raise ValueError("invalid or duplicate evaluation case")
        seen.add(case["id"])
        if mode == "fixture":
            result = await fixture_run(root, case["input"], agent=spec.name)
        elif mode == "sandbox":
            session = "eval-" + uuid.uuid4().hex
            handle = await client.submit(release, session=session, message=case["input"])
            result = await client.wait(handle)
            if result.get("status") == "SUCCEEDED":
                result["files"] = list(await client.read_files(handle))
        else:
            raise ValueError("unknown evaluation mode")
        passed = (
            result.get("status") == "SUCCEEDED"
            and case["contains"] in result.get("result", {}).get("final_text", "")
            and set(case["files"]) <= set(result.get("files", []))
        )
        rows.append(
            {
                "id": case["id"],
                "passed": passed,
                "status": result.get("status"),
                "cleanup": result.get("cleanup"),
            }
        )
    if not rows:
        raise ValueError("empty evaluation suite")
    if dataset != dataset_path.read_bytes():
        raise ValueError("dataset changed during evaluation")
    if source != source_digest(root):
        raise ValueError("source changed during evaluation")
    return {
        "mode": mode,
        "agent": spec.name,
        "agent_sha256": digest(spec.model_dump()),
        "status": "PASSED" if all(r["passed"] for r in rows) else "FAILED",
        "source_sha256": source,
        "release": release.identity if release else None,
        "created_at": datetime.now(UTC).isoformat(),
        "trust": "local-unsigned",
        "sdk": "0.1.0",
        "dataset_sha256": hashlib.sha256(dataset).hexdigest(),
        "model": spec.model,
        "policy": spec.policy,
        "profile": spec.profile,
        "cases": rows,
        "scope": "fixture behavior" if mode == "fixture" else "durable sandbox cases",
    }
