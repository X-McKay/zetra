"""`check` refuses a model grant whose budget is below what the gateway reserves for one
call of its backend: such an agent is denied on its first request."""

import shutil
from pathlib import Path

import pytest
import yaml

from zetra_agents import cli, declaration, render

CORPUS = Path(__file__).parents[1] / "examples" / "declarations" / "valid"
ONE_FILE = "one-file-agent"
TWO_FILE = "tool-backend-grants"


def make_repo(tmp_path, name):
    root = tmp_path / "repo"
    shutil.copytree(CORPUS / name, root)
    (root / "pyproject.toml").write_text('[project]\nname = "corpus"\nversion = "0"\n')
    (root / "uv.lock").write_text("version = 1\n")
    return root


def check(repo, capsys):
    capsys.readouterr()
    code = cli.main(["--repo", str(repo), "check"])
    return code, capsys.readouterr().err.splitlines()


def set_one_file(repo, backend, alias, **budget):
    path = repo / "agents" / ONE_FILE / "agent.yaml"
    text = path.read_text().replace("backend: llm-main", f"backend: {backend}")
    text = text.replace("name: main", f"name: {alias}")
    text = text.replace(
        "budget: {tokensPerDay: 1000000, tokensPerSession: 200000}",
        "budget: {" + ", ".join(f"{k}: {v}" for k, v in budget.items()) + "}",
    )
    path.write_text(text)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "corpus"\nversion = "0"\n\n'
        '[tool.zetra-agents.aliases]\nfast = "llm-fast"\nmain = "llm-main"\nodd = "llm-odd"\n'
    )
    render.write_tools(repo)


@pytest.fixture
def one_file(tmp_path):
    return make_repo(tmp_path, ONE_FILE)


def test_a_session_budget_below_the_reservation_is_refused_and_names_agent_file_and_floor(
    one_file, capsys
):
    set_one_file(one_file, "llm-fast", "fast", tokensPerDay=1000000, tokensPerSession=20000)
    code, problems = check(one_file, capsys)
    assert code == 1
    wanted = (
        f"{ONE_FILE}: tokensPerSession 20000 is below the 32768 tokens the gateway reserves "
        "for each call to llm-fast; no call would be admitted. Raise it to at least 32768 "
        "in agent.yaml (requires.budget)"
    )
    assert problems == [wanted]


def test_a_day_budget_below_the_reservation_is_refused(one_file, capsys):
    set_one_file(one_file, "llm-main", "main", tokensPerDay=100000, tokensPerSession=200000)
    code, problems = check(one_file, capsys)
    assert code == 1 and len(problems) == 1
    assert problems[0].startswith(f"{ONE_FILE}: tokensPerDay 100000 is below the 131072 tokens")
    assert "llm-main" in problems[0]


def test_both_fields_below_are_both_reported(one_file, capsys):
    set_one_file(one_file, "llm-fast", "fast", tokensPerDay=10, tokensPerSession=20)
    code, problems = check(one_file, capsys)
    assert code == 1 and len(problems) == 2


def test_a_budget_equal_to_the_reservation_passes(one_file, capsys):
    set_one_file(one_file, "llm-fast", "fast", tokensPerDay=32768, tokensPerSession=32768)
    assert check(one_file, capsys) == (0, [])


def test_a_backend_outside_the_table_is_not_checked(one_file, capsys):
    set_one_file(one_file, "llm-odd", "odd", tokensPerDay=5, tokensPerSession=5)
    assert check(one_file, capsys) == (0, [])


def test_a_grant_with_no_budget_is_not_checked(one_file, capsys):
    set_one_file(one_file, "llm-fast", "fast", tokensPerDay=1)
    path = one_file / "agents" / ONE_FILE / "agent.yaml"
    path.write_text(path.read_text().replace("  budget: {tokensPerDay: 1}\n", ""))
    render.write_tools(one_file)
    assert check(one_file, capsys) == (0, [])


def test_the_repository_table_replaces_the_built_in_one(one_file, capsys):
    set_one_file(one_file, "llm-fast", "fast", tokensPerDay=1000000, tokensPerSession=20000)
    with (one_file / "pyproject.toml").open("a") as out:
        out.write("\n[tool.zetra-agents.reservations]\nllm-fast = 20000\n")
    assert check(one_file, capsys) == (0, [])
    (one_file / "pyproject.toml").write_text(
        (one_file / "pyproject.toml").read_text().replace("= 20000", "= 20001")
    )
    code, problems = check(one_file, capsys)
    assert code == 1 and "below the 20001 tokens" in problems[0]


@pytest.mark.parametrize("value", ["0", "-5", '"big"', "1.5", "true"])
def test_a_bad_repository_table_falls_back_to_the_built_in(tmp_path, value):
    root = tmp_path
    (root / "pyproject.toml").write_text(f"[tool.zetra-agents.reservations]\nllm-fast = {value}\n")
    assert declaration.reservations(root) == declaration.RESERVATIONS
    assert declaration.reservations(tmp_path / "missing") == declaration.RESERVATIONS


def test_the_two_file_layout_is_checked_in_deploy_agent_yaml(tmp_path, capsys):
    repo = make_repo(tmp_path, TWO_FILE)
    agent = repo / "agents" / TWO_FILE
    cr_path = agent / "deploy" / "agent.yaml"
    cr = yaml.safe_load(cr_path.read_text())
    cr["spec"]["capabilities"]["models"][0]["backend"] = "llm-fast"
    cr["spec"]["capabilities"]["models"][0]["budget"] = {"tokensPerSession": 20000}
    cr_path.write_text(yaml.safe_dump(cr))
    manifest = yaml.safe_load((agent / "agent.yaml").read_text())
    manifest["model"]["backend"] = "llm-fast"
    (agent / "agent.yaml").write_text(yaml.safe_dump(manifest))
    problems = check(repo, capsys)[1]
    wanted = (
        f"{TWO_FILE}: tokensPerSession 20000 is below the 32768 tokens the gateway reserves "
        "for each call to llm-fast; no call would be admitted. Raise it to at least 32768 "
        "in deploy/agent.yaml (spec.capabilities.models[0].budget)"
    )
    assert wanted in problems
