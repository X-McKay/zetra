"""Factories are imported here only in explicit trusted local conformance tests."""

from pathlib import Path

import pytest

from zetra._runtime.artifacts import Receipt
from zetra._runtime.definition import Definition
from zetra._runtime.runtime import Runtime
from zetra.manifest import load_agent


@pytest.mark.parametrize("package,boundaries", [("workspace_reporter", 5), ("document_reader", 3)])
async def test_two_factories_reconstruct_every_accepted_boundary(
    tmp_path, monkeypatch, package, boundaries
):
    package_root = Path("examples") / package
    monkeypatch.syspath_prepend(str(package_root / "src"))
    spec = load_agent(package_root)
    runtime = Runtime(
        tmp_path,
        session="factory",
        manifest="factory-test",
        key=b"k" * 32,
        message="Complete task",
        definition=Definition(spec.model_dump(), tmp_path / "workspace"),
    )
    position = runtime.tape.empty(runtime.workspace.capture())
    try:
        for cursor in range(boundaries + 1):
            command = {
                "binding": {"session": "factory"},
                "position": position.model_dump(),
                "pending": None,
            }
            warm = await runtime.handle(runtime.authority.sign("advance", command))
            before = len(runtime.effects)
            await runtime.warm.close()
            cold = await runtime.handle(runtime.authority.sign("advance", command))
            assert cold == warm
            assert len(runtime.effects) == before
            if cursor == boundaries:
                assert cold["status"] == "done"
                break
            executed = await runtime.handle(
                runtime.authority.sign(
                    "execute",
                    {**command, "operation": cold["operation"], "attempt_id": str(cursor)},
                )
            )
            position = Receipt.model_validate(executed["receipt"]).position
        assert len(runtime.effects) == boundaries
    finally:
        await runtime.close()
