"""The corpus in `examples/declarations/` is what an agent may declare, and this keeps it true.

Each example is a repository fragment, `<example>/agents/<agent>/`, holding the agent's
declaration files (and whatever stub `check` insists on). It is judged by `zetra-agents
check` itself, run on a copy of it with the repository-level stubs added, so a rule that
changes in the checking code changes what these examples do.

`valid/<example>/` must pass. `invalid/<example>/` must fail, and `invalid/<example>.expect.json`
says how: `error` is a substring of the one problem `check` prints (`problems` gives another
count when a rule cannot be broken in a single line, and `error` is then a list holding a
substring for each of those lines), and `rule` is the sentence a reader sees.
The repository's own concerns (workspace, lock, rendered files) are not in the corpus.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from zetra_agents import cli, layout

CORPUS = Path(__file__).parents[1] / "examples" / "declarations"
VALID = sorted(p.name for p in (CORPUS / "valid").iterdir() if p.is_dir())
INVALID = sorted(p.name for p in (CORPUS / "invalid").iterdir() if p.is_dir())
EXPECTATIONS = sorted(
    p.name.removesuffix(".expect.json") for p in (CORPUS / "invalid").glob("*.json")
)


def check(example: Path, tmp_path: Path, capsys, monkeypatch) -> tuple[int, list[str]]:
    """Run `zetra-agents check` on a copy of the example as the only agent of a repository."""
    monkeypatch.delenv("AGENT_PLATFORM_MCP_RESOURCE_BASE", raising=False)
    repo = tmp_path / "repo"
    shutil.copytree(example, repo)
    (repo / "pyproject.toml").write_text('[project]\nname = "corpus"\nversion = "0"\n')
    (repo / "uv.lock").write_text("version = 1\n")
    capsys.readouterr()
    code = cli.main(["--repo", str(repo), "check"])
    return code, capsys.readouterr().err.splitlines()


def test_the_corpus_is_not_empty():
    assert len(VALID) >= 5
    assert INVALID


def test_every_invalid_example_has_an_expectation_and_the_reverse():
    assert INVALID == EXPECTATIONS, (
        f"without an expectation: {sorted(set(INVALID) - set(EXPECTATIONS))}; "
        f"without an example: {sorted(set(EXPECTATIONS) - set(INVALID))}"
    )


@pytest.mark.parametrize("name", VALID)
def test_a_valid_example_passes_check(name, tmp_path, capsys, monkeypatch):
    code, problems = check(CORPUS / "valid" / name, tmp_path, capsys, monkeypatch)
    assert (code, problems) == (0, [])


@pytest.mark.parametrize("name", INVALID)
def test_an_invalid_example_fails_check_as_expected(name, tmp_path, capsys, monkeypatch):
    expect = json.loads((CORPUS / "invalid" / f"{name}.expect.json").read_text())
    assert set(expect) <= {"rule", "error", "problems"}
    errors = [expect["error"]] if isinstance(expect["error"], str) else expect["error"]
    assert expect["rule"].strip() and errors and all(e.strip() for e in errors)
    assert len(errors) == expect.get("problems", 1)
    code, problems = check(CORPUS / "invalid" / name, tmp_path, capsys, monkeypatch)
    assert code == 1, problems
    assert len(problems) == expect.get("problems", 1), problems
    for error in errors:
        assert any(error in p for p in problems), (error, problems)


def test_the_effects_check_admits_are_the_ones_the_sdk_defines():
    from agent_runtime import tools

    assert set(layout.EFFECTS) == set(tools.EFFECTS)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_a_declaration_that_cannot_be_read_is_one_problem_line(tmp_path, capsys, monkeypatch):
    example = CORPUS / "valid" / "minimal-model-agent"
    repo = tmp_path / "repo"
    shutil.copytree(example, repo)
    (repo / "pyproject.toml").write_text('[project]\nname = "corpus"\nversion = "0"\n')
    (repo / "uv.lock").write_text("version = 1\n")
    unreadable = repo / "agents" / "minimal-model-agent" / "deploy" / "agent.yaml"
    unreadable.chmod(0)
    capsys.readouterr()
    try:
        code = cli.main(["--repo", str(repo), "check"])
    finally:
        unreadable.chmod(0o644)
    problems = capsys.readouterr().err.splitlines()
    assert code == 1
    assert problems == [
        "minimal-model-agent: deploy/agent.yaml cannot be read: Permission denied"
    ], problems
