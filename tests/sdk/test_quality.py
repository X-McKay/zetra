import json
from unittest.mock import AsyncMock

import pytest

from zetra.quality import explain_quality, run_quality


def project(root, test="def test_behavior():\n    assert 2 + 2 == 4\n"):
    package = root / "src/sample"
    package.mkdir(parents=True)
    (package / "agent.yaml").write_text(
        "name: reader\nowner: test\nentrypoint: sample:create_agent\n"
    )
    (package / "__init__.py").write_text('raise AssertionError("static check imported code")')
    (root / "pyproject.toml").write_text('[tool.zetra.agents]\nreader = "src/sample/agent.yaml"\n')
    (root / "tests").mkdir()
    (root / "tests/test_behavior.py").write_text(test)
    directory = root / "evals/reader"
    directory.mkdir(parents=True)
    (directory / "cases.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "id": "basic",
                "input": "Read.",
                "assertions": [
                    {"type": "text_contains", "value": "Done"},
                    {"type": "max_steps", "maximum": 5},
                ],
            }
        )
        + "\n"
    )
    contract = {
        "schema_version": 1,
        "agent": "reader",
        "tools": {},
        "skills": {},
        "live_model_required": False,
        "requirements": [
            {"id": "answer", "component": "agent", "kind": "outcome", "cases": ["basic"]},
            {"id": "budget", "component": "agent", "kind": "performance", "cases": ["basic"]},
            {
                "id": "unit",
                "component": "agent",
                "kind": "contract",
                "tests": ["tests/test_behavior.py::test_behavior"],
            },
        ],
    }
    (directory / "quality.json").write_text(json.dumps(contract))
    return contract


def save(root, contract):
    (root / "evals/reader/quality.json").write_text(json.dumps(contract))


def evaluation():
    return {
        "status": "PASSED",
        "counts": {"planned": 1, "passed": 1, "failed": 0, "error": 0},
        "cases": [
            {
                "id": "basic",
                "passed": True,
                "status": "SUCCEEDED",
                "outcome": "passed",
                "tools": {},
                "assertions": [
                    {"type": "text_contains", "passed": True},
                    {"type": "max_steps", "passed": True},
                ],
            }
        ],
    }


def test_static_explanation_does_not_import_factory(tmp_path):
    project(tmp_path)
    report = explain_quality(tmp_path)
    assert report["status"] == "PASSED"
    assert report["missing"] == []
    assert len(report["contract_sha256"]) == 64


@pytest.mark.parametrize(
    "change", ["missing", "unknown", "security-case", "bad-selector", "tool", "skill"]
)
def test_incomplete_contract_fails_before_execution(tmp_path, change):
    contract = project(tmp_path)
    if change == "missing":
        contract["requirements"].pop(1)
    elif change == "unknown":
        contract["extra"] = True
    elif change == "security-case":
        contract["requirements"][0]["kind"] = "security"
    elif change == "bad-selector":
        contract["requirements"][2]["tests"] = ["--override-ini=x"]
    elif change == "tool":
        contract["tools"] = {"read": "workspace_read"}
    else:
        contract["skills"] = {"skill": "sample:missing.md"}
    save(tmp_path, contract)
    assert explain_quality(tmp_path)["status"] == "FAILED"


async def test_runs_fresh_pytest_and_checks_evaluation(tmp_path, monkeypatch):
    project(tmp_path)
    mock = AsyncMock(return_value=evaluation())
    monkeypatch.setattr("zetra.evaluation.evaluate", mock)
    result = await run_quality(tmp_path)
    assert result["status"] == "PASSED", result
    assert result["trust"] == "local-unsigned"
    assert result["pytest"]["collected"] == ["tests/test_behavior.py::test_behavior"]
    mock.assert_awaited_once()


@pytest.mark.parametrize(
    "body",
    [
        'import pytest\n@pytest.mark.skip(reason="unavailable")\ndef test_behavior(): pass\n',
        'import pytest\n@pytest.mark.xfail(reason="known")\ndef test_behavior(): assert False\n',
        'import pytest\n@pytest.mark.xfail(reason="known")\ndef test_behavior(): pass\n',
        "import pytest\n@pytest.fixture(autouse=True)\ndef broken():\n    yield\n    assert False\ndef test_behavior(): pass\n",
    ],
)
async def test_skips_xfails_xpasses_teardown_failures_block(tmp_path, monkeypatch, body):
    project(tmp_path, body)
    monkeypatch.setattr("zetra.evaluation.evaluate", AsyncMock(return_value=evaluation()))
    assert (await run_quality(tmp_path))["status"] == "FAILED"


async def test_incomplete_requirements_never_execute(tmp_path, monkeypatch):
    contract = project(tmp_path)
    contract["requirements"] = []
    save(tmp_path, contract)
    mock = AsyncMock(side_effect=AssertionError("must not execute"))
    monkeypatch.setattr("zetra.evaluation.evaluate", mock)
    assert (await run_quality(tmp_path))["status"] == "FAILED"
    mock.assert_not_awaited()


async def test_mutating_tests_fails_evidence_identity(tmp_path, monkeypatch):
    project(
        tmp_path,
        'from pathlib import Path\ndef test_behavior():\n    Path(__file__).write_text("# changed")\n',
    )
    monkeypatch.setattr("zetra.evaluation.evaluate", AsyncMock(return_value=evaluation()))
    result = await run_quality(tmp_path)
    assert result["status"] == "FAILED"
    assert any("changed" in problem for problem in result["missing"])


async def test_live_requirement_fails_before_tests(tmp_path, monkeypatch):
    contract = project(tmp_path)
    contract["live_model_required"] = True
    save(tmp_path, contract)
    monkeypatch.setattr("zetra.quality._run_pytest", lambda *args: pytest.fail("must not execute"))
    result = await run_quality(tmp_path)
    assert result["status"] == "FAILED"
    assert result["pytest"] is None


async def test_parametrized_selector_expansion_and_missing_node(tmp_path, monkeypatch):
    project(
        tmp_path,
        'import pytest\n@pytest.mark.parametrize("number", [1, 2])\ndef test_behavior(number):\n    assert number > 0\n',
    )
    monkeypatch.setattr("zetra.evaluation.evaluate", AsyncMock(return_value=evaluation()))
    result = await run_quality(tmp_path)
    assert result["status"] == "PASSED", result
    assert len(result["pytest"]["collected"]) == 2
    contract = json.loads((tmp_path / "evals/reader/quality.json").read_text())
    contract["requirements"][2]["tests"] = ["tests/test_behavior.py::test_missing"]
    save(tmp_path, contract)
    assert (await run_quality(tmp_path))["status"] == "FAILED"


async def test_unlisted_inventory_blocks_otherwise_passing_evidence(tmp_path, monkeypatch):
    project(tmp_path)
    report = evaluation()
    report["cases"][0]["tools"] = {"surprise": "remote_read"}
    monkeypatch.setattr("zetra.evaluation.evaluate", AsyncMock(return_value=report))
    result = await run_quality(tmp_path)
    assert result["status"] == "FAILED"
    assert any("inventory" in problem for problem in result["missing"])


async def test_real_fixture_evaluation(tmp_path):
    project(tmp_path)
    (tmp_path / "src/sample/__init__.py").write_text(
        "from google.adk.agents import LlmAgent\n"
        "def create_agent(ctx):\n"
        "    return LlmAgent(name='reader', model=ctx.model, tools=[])\n"
    )
    cases = tmp_path / "evals/reader/cases.jsonl"
    cases.write_text(cases.read_text().replace('"Done"', '"Completed."'))
    result = await run_quality(tmp_path)
    assert result["status"] == "PASSED", result


@pytest.mark.parametrize(
    "edit", ["duplicate", "bool-version", "both", "wrong-kind", "unknown-case"]
)
def test_strict_contract_fields_and_evidence_types(tmp_path, edit):
    contract = project(tmp_path)
    if edit == "duplicate":
        contract["requirements"].append(contract["requirements"][0])
    elif edit == "bool-version":
        contract["schema_version"] = True
    elif edit == "both":
        contract["requirements"][0]["tests"] = ["tests/test_behavior.py::test_behavior"]
    elif edit == "wrong-kind":
        contract["requirements"][0]["kind"] = "trajectory"
    else:
        contract["requirements"][0]["cases"] = ["missing"]
    save(tmp_path, contract)
    assert explain_quality(tmp_path)["status"] == "FAILED"


def test_complete_tool_and_skill_requirements(tmp_path):
    contract = project(tmp_path)
    contract["tools"] = {"read": "workspace_read"}
    contract["skills"] = {"incident": "sample:incident.md"}
    (tmp_path / "src/sample/incident.md").write_text("Investigate with evidence.")
    selector = ["tests/test_behavior.py::test_behavior"]
    for component, kinds in [
        ("agent", ["trajectory"]),
        ("tool:read", ["contract", "security", "recovery"]),
        ("skill:incident", ["contract", "activation", "nonactivation"]),
    ]:
        for kind in kinds:
            contract["requirements"].append(
                {
                    "id": component.replace(":", "-") + "-" + kind,
                    "component": component,
                    "kind": kind,
                    "tests": selector,
                }
            )
    save(tmp_path, contract)
    assert explain_quality(tmp_path)["status"] == "PASSED"
    contract["requirements"].pop()
    save(tmp_path, contract)
    result = explain_quality(tmp_path)
    assert result["status"] == "FAILED"
    assert any("skill:incident/nonactivation" in missing for missing in result["missing"])


async def test_async_project_pytest_plugin_remains_available(tmp_path, monkeypatch):
    project(
        tmp_path,
        "import pytest\n@pytest.mark.asyncio\nasync def test_behavior():\n    assert True\n",
    )
    monkeypatch.setattr("zetra.evaluation.evaluate", AsyncMock(return_value=evaluation()))
    result = await run_quality(tmp_path)
    assert result["status"] == "PASSED", result


async def test_nested_agent_project_keeps_project_relative_pytest_nodeids(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["agents"]\n')
    nested = tmp_path / "agents/reader"
    project(nested)
    monkeypatch.setattr("zetra.evaluation.evaluate", AsyncMock(return_value=evaluation()))
    result = await run_quality(nested)
    assert result["status"] == "PASSED", result
    assert result["pytest"]["collected"] == ["tests/test_behavior.py::test_behavior"]


async def test_pytest_output_secrets_are_not_persisted(tmp_path, monkeypatch):
    project(
        tmp_path,
        'import sys\ndef test_behavior():\n    print("private-test-secret")\n    print("private-stderr-secret", file=sys.stderr)\n    assert False\n',
    )
    monkeypatch.setattr("zetra.evaluation.evaluate", AsyncMock(return_value=evaluation()))
    result = await run_quality(tmp_path)
    assert result["status"] == "FAILED"
    persisted = json.dumps(result)
    assert "private-test-secret" not in persisted
    assert "private-stderr-secret" not in persisted
    assert "stdout" not in result["pytest"]
    assert "stderr" not in result["pytest"]


@pytest.mark.parametrize("selection_source", ["environment", "project"])
async def test_generic_requirement_includes_deselected_parameters(
    tmp_path, monkeypatch, selection_source
):
    contract = project(
        tmp_path,
        'import pytest\n@pytest.mark.parametrize("name", ["one", "two"], ids=["one", "two"])\n'
        'def test_behavior(name):\n    assert name == "one"\n',
    )
    if selection_source == "environment":
        monkeypatch.setenv("PYTEST_ADDOPTS", "-k one")
    else:
        config = tmp_path / "pyproject.toml"
        config.write_text(config.read_text() + '\n[tool.pytest.ini_options]\naddopts = "-k one"\n')
    monkeypatch.setattr("zetra.evaluation.evaluate", AsyncMock(return_value=evaluation()))
    result = await run_quality(tmp_path)
    assert result["status"] == "FAILED", result
    assert result["pytest"]["deselected"] == ["tests/test_behavior.py::test_behavior[two]"]
    contract["requirements"][2]["tests"] = ["tests/test_behavior.py::test_behavior[one]"]
    save(tmp_path, contract)
    explicit = await run_quality(tmp_path)
    assert explicit["status"] == "PASSED", explicit


async def test_evaluation_exception_message_is_not_persisted(tmp_path, monkeypatch):
    project(tmp_path)
    monkeypatch.setattr(
        "zetra.evaluation.evaluate", AsyncMock(side_effect=ValueError("private-exception-secret"))
    )
    result = await run_quality(tmp_path)
    assert result["status"] == "FAILED"
    assert "private-exception-secret" not in json.dumps(result)
    assert "evaluation error: ValueError" in result["missing"]


@pytest.mark.parametrize("failing_call", [1, 2])
async def test_input_exception_message_is_not_persisted(tmp_path, monkeypatch, failing_call):
    project(tmp_path)
    monkeypatch.setattr("zetra.evaluation.evaluate", AsyncMock(return_value=evaluation()))
    from zetra import quality

    original = quality._inputs
    calls = 0

    def inputs(*args):
        nonlocal calls
        calls += 1
        if calls == failing_call:
            raise OSError("private-input-secret")
        return original(*args)

    monkeypatch.setattr(quality, "_inputs", inputs)
    result = await run_quality(tmp_path)
    assert result["status"] == "FAILED"
    assert "private-input-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "mode,config,nonfixture",
    [
        ("local", None, False),
        ("fixture", {}, False),
        ("fixture", None, True),
        ("local", {"unknown": "private-config-secret"}, False),
    ],
)
async def test_invalid_execution_configuration_fails_before_tests(
    tmp_path, monkeypatch, mode, config, nonfixture
):
    project(tmp_path)
    if nonfixture:
        manifest = tmp_path / "src/sample/agent.yaml"
        manifest.write_text(manifest.read_text() + "model: vllm\npolicy: vllm-chat\n")
    monkeypatch.setattr(
        "zetra.quality._run_pytest", lambda *args: pytest.fail("must not execute tests")
    )
    monkeypatch.setattr(
        "zetra.evaluation.evaluate", AsyncMock(side_effect=AssertionError("must not evaluate"))
    )
    result = await run_quality(tmp_path, mode=mode, model_config=config)
    assert result["status"] == "FAILED"
    assert result["pytest"] is None
    assert "private-config-secret" not in json.dumps(result)
