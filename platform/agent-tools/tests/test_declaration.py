"""The one-file layout, the model alias, the evaluation suite and the carried declaration
(delivery.md sections 1 and 2)."""

import copy
import json
import shutil
from pathlib import Path

import pytest
import yaml
from agent_platform.bundle import compile_bundle, digest_hex

from zetra_agents import cli, declaration, layout, release, render, suite

CORPUS = Path(__file__).parents[1] / "examples" / "declarations" / "valid"
EXAMPLE = CORPUS / "one-file-agent"
NAME = "one-file-agent"
CASES = json.loads(
    (Path(__file__).parents[2] / "registry" / "contract" / "suite-cases.json").read_text()
)
BASE = "https://mcp.almckay.io"
IMAGE = f"registry.almckay.io/agents/{NAME}@sha256:" + "a9" * 32


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(EXAMPLE, root)
    (root / "pyproject.toml").write_text('[project]\nname = "corpus"\nversion = "0"\n')
    (root / "uv.lock").write_text("version = 1\n")
    return root


def agent_of(repo):
    return layout.AgentDir(repo / "agents" / NAME)


def edit_manifest(repo, change):
    path = repo / "agents" / NAME / "agent.yaml"
    path.write_text(change(path.read_text()))


def check(repo, capsys):
    capsys.readouterr()
    code = cli.main(["--repo", str(repo), "check"])
    return code, capsys.readouterr().err.splitlines()


def test_the_one_file_example_passes_check(repo, capsys):
    assert check(repo, capsys) == (0, [])


@pytest.mark.parametrize(
    ("name", "backend", "found"),
    [
        ("slow", "llm-main", "'slow' is not an alias"),
        ("example-model", "llm-main", "'example-model' is not an alias"),
        ("fast", "llm-main", "'fast' is an alias of the backend 'llm-fast', not 'llm-main'"),
    ],
)
def test_a_model_name_that_is_not_an_alias_of_the_backend_names_file_line_and_aliases(
    repo, capsys, name, backend, found
):
    edit_manifest(repo, lambda t: t.replace("name: main", f"name: {name}"))
    render.write_tools(repo)  # so the generated file is current and only the alias is wrong
    code, problems = check(repo, capsys)
    assert code == 1
    line = next(
        i
        for i, text in enumerate((agent_of(repo).manifest_path).read_text().splitlines(), 1)
        if text.strip() == f"name: {name}"
    )
    assert len(problems) == 1
    assert f"agents/{NAME}/agent.yaml:{line}:" in problems[0]
    assert found in problems[0]
    assert "it may be 'main'" in problems[0]


def test_the_alias_table_of_pyproject_replaces_the_built_in_one(repo, capsys):
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "corpus"\nversion = "0"\n\n[tool.zetra-agents.aliases]\nbig = "llm-main"\n'
    )
    code, problems = check(repo, capsys)
    assert code == 1 and "model.name 'main' is not an alias" in problems[0]
    assert "it may be 'big'" in problems[0]
    edit_manifest(repo, lambda t: t.replace("name: main", "name: big"))
    render.write_tools(repo)
    assert check(repo, capsys) == (0, [])


def test_the_generated_agent_is_the_expected_one_field_by_field(repo):
    manifest = agent_of(repo).manifest()
    assert declaration.generate(manifest) == {
        "apiVersion": "agentplatform.io/v1alpha1",
        "kind": "Agent",
        "metadata": {"name": NAME, "namespace": "tenant-example"},
        "spec": {
            "owner": "dev@example.com",
            "description": "Reads one report.",
            "identity": {"mode": "agent"},
            "capabilities": {
                "models": [
                    {
                        "backend": "llm-main",
                        "models": ["main"],
                        "budget": {"tokensPerDay": 1000000, "tokensPerSession": 200000},
                    }
                ],
                "mcp": [
                    {
                        "backend": "postgres-admin",
                        "effect": "remote_read",
                        "tools": ["execute_sql"],
                        "delegation": "inherit",
                    }
                ],
            },
            "runtime": {"cpu": "1", "memory": "2Gi", "ttlSeconds": 900},
        },
    }
    text = agent_of(repo).agent_cr_path.read_text()
    assert text.startswith("# Written by `zetra-agents render` from agent.yaml; not edited")
    assert yaml.safe_load(text) == declaration.generate(manifest)


def test_a_manifest_with_no_model_generates_an_empty_models_list(repo):
    manifest = agent_of(repo).manifest()
    manifest["model"] = None
    assert declaration.generate(manifest)["spec"]["capabilities"]["models"] == []


def test_a_stale_or_missing_generated_file_fails_check_until_the_command_is_run(repo, capsys):
    cr = agent_of(repo).agent_cr_path
    cr.write_text(cr.read_text().replace("2Gi", "4Gi"))
    code, problems = check(repo, capsys)
    assert code == 1 and len(problems) == 1 and "run `zetra-agents render`" in problems[0]
    assert render.write_tools(repo) == [cr]
    assert check(repo, capsys) == (0, [])
    cr.unlink()
    code, problems = check(repo, capsys)
    assert code == 1 and "missing" in problems[0] and "run `zetra-agents render`" in problems[0]
    assert "`zetra-agents render`" in next(d.detail for d in render.tool_problems(repo))
    assert render.write_tools(repo) == [cr]
    assert check(repo, capsys) == (0, [])
    assert render.tool_problems(repo) == []


def test_an_unknown_key_of_requires_is_an_error_naming_it(repo, capsys):
    edit_manifest(repo, lambda t: t.replace("  tenant:", "  colour: red\n  tenant:"))
    code, problems = check(repo, capsys)
    assert code == 1
    assert len(problems) == 1 and "requires.colour" in problems[0]


def test_an_agent_without_requires_generates_nothing_and_is_checked_as_before(repo, capsys):
    two = repo / "agents" / NAME
    manifest = yaml.safe_load((two / "agent.yaml").read_text())
    cr = declaration.generate(manifest)
    del manifest["requires"]
    manifest["model"]["name"] = "example-model"
    cr["spec"]["capabilities"]["models"][0]["models"] = ["example-model"]
    (two / "agent.yaml").write_text(yaml.safe_dump(manifest))
    (two / "deploy" / "agent.yaml").write_text(yaml.safe_dump(cr))
    before = (two / "deploy" / "agent.yaml").read_text()
    assert check(repo, capsys) == (0, [])
    assert render.write_tools(repo) == []
    assert (two / "deploy" / "agent.yaml").read_text() == before
    (two / "deploy" / "agent.yaml").unlink()
    code, problems = check(repo, capsys)
    assert code == 1 and problems == [f"{NAME}: missing deploy/agent.yaml"]


def test_every_suite_the_registry_accepts_is_accepted(repo):
    for case in CASES["valid"]:
        assert suite.problems(case["suite"]) == [], case["name"]


def test_every_suite_the_registry_refuses_is_refused_naming_its_key():
    for case in CASES["invalid"]:
        found = suite.problems(case["suite"])
        assert found, case["name"]
        assert case["mentions"] in " ".join(found), (case["name"], found)


def test_check_applies_the_suite_rules_to_the_suite_file(repo, capsys):
    path = repo / "agents" / NAME / "evaluation" / "suite.yaml"
    path.write_text(path.read_text().replace("timeout_seconds: 600", "timeout_seconds: 5"))
    code, problems = check(repo, capsys)
    assert code == 1
    assert problems == [
        f"{NAME}: evaluation/suite.yaml: timeout_seconds: required, a whole number from 30 to 3600"
    ]


def test_the_release_carries_the_declaration_and_its_policy_is_unchanged(repo):
    artifacts = release.build(
        agent_of(repo),
        image=IMAGE,
        revision="a" * 40,
        mcp_resource_base=BASE,
        trust_domain="cluster.local",
    )
    spec = artifacts["agent"]["spec"]
    carried = artifacts["release"]["spec"]["declaration"]
    assert carried == {k: v for k, v in spec.items() if k != "enabled"}
    with_enabled = copy.deepcopy(artifacts["agent"])
    with_enabled["spec"]["enabled"] = True
    again = release.build(
        agent_of(repo),
        image=IMAGE,
        revision="a" * 40,
        mcp_resource_base=BASE,
        trust_domain="cluster.local",
    )
    assert again["release"]["spec"] == artifacts["release"]["spec"]
    without = copy.deepcopy(artifacts["release"])
    del without["spec"]["declaration"]
    bundle = compile_bundle(
        artifacts["agent"], without, mcp_resource_base=BASE, trust_domain="cluster.local"
    )
    assert artifacts["release"]["spec"]["policySha256"] == digest_hex(bundle)


def test_the_runtime_loads_a_manifest_with_a_requires_block(monkeypatch):
    from agent_runtime import step

    path = EXAMPLE / "agents" / NAME
    monkeypatch.setenv("AGENT_APP_DIR", str(path))
    loaded = step.load_manifest()
    assert loaded["requires"]["tenant"] == "tenant-example"
    assert loaded["model"]["name"] == "main"
