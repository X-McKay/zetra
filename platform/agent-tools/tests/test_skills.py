"""Skills in the tooling: what `check` holds, the lock `render` writes, the release's
`spec.skills`, the local harness's layout and the Dockerfile (skills.md sections 4, 6, 8.1,
11.1 and 12)."""

import json
import shutil
from pathlib import Path

import pytest
import yaml
from agent_runtime.skills import format as fmt

from zetra_agents import cli, layout, local, release, render, skills, testing

EXAMPLE = Path(__file__).parents[1] / "examples" / "declarations" / "valid" / "one-file-agent"
NAME = "one-file-agent"
BASE = "https://mcp.almckay.io"
IMAGE = f"registry.almckay.io/agents/{NAME}@sha256:" + "a9" * 32
SIDECAR = """\
schema_version: 1
requires:
  tools:
    - backend: postgres-admin
      tools: [execute_sql]
scripts:
  - path: scripts/count.py
    interpreter: python3
"""


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(EXAMPLE, root)
    (root / "pyproject.toml").write_text('[project]\nname = "corpus"\nversion = "0"\n')
    (root / "uv.lock").write_text("version = 1\n")
    return root


def agent_of(repo):
    return layout.AgentDir(repo / "agents" / NAME)


def make_skill(repo, name="row-count", *, sidecar=SIDECAR, body="Count rows.\n"):
    directory = repo / "skills" / name
    (directory / "scripts").mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Counts rows in a table.\nmetadata:\n  version: '1.2'\n"
        f"---\n{body}"
    )
    (directory / "scripts" / "count.py").write_text("print(1)\n")
    if sidecar is not None:
        (directory / "zetra.yaml").write_text(sidecar)
    return directory


def edit_manifest(repo, change):
    path = repo / "agents" / NAME / "agent.yaml"
    manifest = yaml.safe_load(path.read_text())
    change(manifest)
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))


def list_skills(repo, *names, effect=True):
    def change(manifest):
        manifest["skills"] = list(names)
        if effect:
            manifest["requires"]["effects"] = ["sandbox_execute"]

    edit_manifest(repo, change)
    render.write_tools(repo)


def check(repo, capsys):
    capsys.readouterr()
    code = cli.main(["--repo", str(repo), "check"])
    captured = capsys.readouterr()
    return code, captured.err, captured.out


def test_an_agent_without_skills_is_unchanged_and_has_no_lock(repo, capsys):
    assert check(repo, capsys)[0] == 0
    assert render.write_tools(repo) == []
    assert not (repo / "agents" / NAME / "skills.lock").exists()
    assert "skills" not in release.build(
        agent_of(repo), image=IMAGE, revision="a" * 40, mcp_resource_base=BASE,
        trust_domain="cluster.local",
    )["release"]["spec"]  # fmt: skip


def test_a_skill_that_needs_a_tool_the_agent_lacks_fails_with_the_lines_to_add(repo, capsys):
    make_skill(
        repo,
        sidecar=SIDECAR.replace("postgres-admin", "qdrant").replace("execute_sql", "search"),
    )
    list_skills(repo, "row-count")
    code, err, _ = check(repo, capsys)
    assert code == 1
    assert (
        f"{NAME}: skill row-count needs tools the agent does not request; add to agent.yaml:" in err
    )
    assert "  requires:\n    tools:\n    - backend: qdrant\n      tools: [search]" in err


def test_a_skill_with_no_sidecar_is_undeclared_and_offers_no_scripts(repo, capsys):
    make_skill(repo, sidecar=None)
    list_skills(repo, "row-count", effect=False)
    render.write_tools(repo)
    code, err, out = check(repo, capsys)
    assert code == 0, err
    assert (
        f"{NAME}: skill row-count has no zetra.yaml: its tool needs are unknown and its "
        "scripts do not run" in out
    )
    (entry,) = skills.release_entries(agent_of(repo))
    assert entry["declared"] is False and "scripts" not in entry
    # no scripts declared: asking for the effect is refused
    list_skills(repo, "row-count", effect=True)
    code, err, _ = check(repo, capsys)
    assert code == 1 and "no listed skill declares scripts" in err


def test_a_changed_skill_without_a_new_lock_fails_and_render_repairs_it(repo, capsys):
    make_skill(repo)
    list_skills(repo, "row-count")
    assert check(repo, capsys)[0] == 0
    (repo / "skills" / "row-count" / "scripts" / "count.py").write_text("print(2)\n")
    code, err, _ = check(repo, capsys)
    assert code == 1
    assert "skill row-count changed since it was pinned in skills.lock" in err
    assert "zetra-agents render" in err
    (stale,) = render.tool_problems(repo)
    assert stale.path == f"agents/{NAME}/skills.lock" and "zetra-agents render" in stale.detail
    assert render.write_tools(repo) == [skills.lock_path(agent_of(repo))]
    assert check(repo, capsys)[0] == 0
    assert render.tool_problems(repo) == []


def test_render_writes_the_lock_and_two_agents_pin_one_digest(repo):
    make_skill(repo)
    other = repo / "agents" / "second"
    shutil.copytree(repo / "agents" / NAME, other)
    manifest = yaml.safe_load((other / "agent.yaml").read_text())
    manifest["name"] = "second"
    (other / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    list_skills(repo, "row-count")
    edit = yaml.safe_load((other / "agent.yaml").read_text())
    edit["skills"] = ["row-count"]
    edit["requires"]["effects"] = ["sandbox_execute"]
    (other / "agent.yaml").write_text(yaml.safe_dump(edit, sort_keys=False))
    render.write_tools(repo)
    first = json.loads((repo / "agents" / NAME / "skills.lock").read_text())
    second = json.loads((other / "skills.lock").read_text())
    assert first == second
    assert first["skills"][0]["digest"] == fmt.digest(repo / "skills" / "row-count")
    assert (repo / "agents" / NAME / "skills.lock").read_text() == fmt.lock_text(
        [fmt.read(repo / "skills" / "row-count")]
    )
    # listing none again removes the lock
    edit_manifest(repo, lambda m: (m.pop("skills"), m["requires"].pop("effects")))
    render.write_tools(repo)
    assert not (repo / "agents" / NAME / "skills.lock").exists()


@pytest.mark.parametrize(
    ("scripts", "effect", "found"),
    [
        (True, False, "declares scripts, which run only with the effect sandbox_execute"),
        (False, True, "no listed skill declares scripts"),
        (True, True, None),
        (False, False, None),
    ],
)
def test_the_effect_is_requested_exactly_when_scripts_are_declared(
    repo, capsys, scripts, effect, found
):
    make_skill(repo, sidecar=SIDECAR if scripts else "schema_version: 1\n")
    list_skills(repo, "row-count", effect=effect)
    code, err, _ = check(repo, capsys)
    if found is None:
        assert code == 0, err
    else:
        assert code == 1 and found in err
        if scripts:
            assert "effects: [sandbox_execute]" in err


def test_a_two_file_agent_with_skills_is_refused(repo, capsys):
    make_skill(repo)
    edit_manifest(repo, lambda m: (m.pop("requires"), m.update(skills=["row-count"])))
    code, err, _ = check(repo, capsys)
    assert code == 1
    assert f"{NAME}: agent.yaml lists skills and has no requires block" in err


def test_the_release_carries_spec_skills_in_name_order(repo):
    make_skill(repo, "row-count")
    make_skill(repo, "alpha", sidecar="schema_version: 1\n")
    list_skills(repo, "row-count", "alpha")
    artifacts = release.build(
        agent_of(repo), image=IMAGE, revision="a" * 40, mcp_resource_base=BASE,
        trust_domain="cluster.local",
    )  # fmt: skip
    carried = artifacts["release"]["spec"]["skills"]
    assert [e["name"] for e in carried] == ["alpha", "row-count"]
    assert carried[1] == fmt.release_entry(fmt.read(repo / "skills" / "row-count"))
    assert carried[1]["scripts"] == ["scripts/count.py"] and carried[1]["declared"] is True
    assert artifacts["release"]["spec"]["declaration"]["capabilities"]["effects"] == [
        "sandbox_execute"
    ]


def test_the_local_harness_lays_skills_out_and_sets_the_effect(repo, tmp_path):
    make_skill(repo)
    list_skills(repo, "row-count")
    agent = agent_of(repo)
    app = testing.lay_out(agent, tmp_path / "app", with_skills=True)
    assert (app / "skills" / "row-count" / "SKILL.md").is_file()
    assert (app / "skills.lock").read_text() == (agent.root / "skills.lock").read_text()
    assert testing.effects_environment(agent) == {"AGENT_EFFECTS": "sandbox_execute"}
    env = testing.child_environment(app, tmp_path, **testing.effects_environment(agent))
    assert env["AGENT_EFFECTS"] == "sandbox_execute"


def test_a_stale_lock_stops_the_local_layout(repo, tmp_path):
    make_skill(repo)
    list_skills(repo, "row-count")
    (repo / "skills" / "row-count" / "scripts" / "count.py").write_text("print(2)\n")
    with pytest.raises(ValueError, match="skills cannot be staged"):
        testing.lay_out(agent_of(repo), tmp_path / "app", with_skills=True)


SCRIPT_RUN = {
    "skill": "row-count",
    "script": "scripts/count.py",
    "digest": "sha256:" + "ab" * 32,
    "argument_count": 2,
    "arguments_sha256": "cd" * 32,
    "exit_status": 0,
    "limits_hit": [],
    "refused": None,
    "duration_ms": 41,
}


def result(tool, **extra):
    doc = {"boundary": 3, "kind": "tool", "done": False, "tool": tool, "usage": None}
    return json.dumps({**doc, "summary": "x", **extra}).encode()


def test_the_local_harness_reads_the_script_run_key_of_a_skill_run_boundary():
    accepted = local.step_result(result("skill_run", script_run=SCRIPT_RUN), 3, 0)
    assert accepted["script_run"] == SCRIPT_RUN
    assert (
        local._detail(accepted, {})
        == "script_run row-count/scripts/count.py exit 0, 2 arguments, 41 ms"
    )
    # on no other boundary, and not missing from this one
    assert local.step_result(result("other", script_run=SCRIPT_RUN), 3, 0) is None
    assert local.step_result(result("skill_run"), 3, 0) is None
    assert (
        local.step_result(result("skill_run", script_run={**SCRIPT_RUN, "extra": 1}), 3, 0) is None
    )
    assert (
        local.step_result(result("skill_run", script_run={**SCRIPT_RUN, "limits_hit": ["x"]}), 3, 0)
        is None
    )
    assert local.step_result(result("other"), 3, 0) is not None


def test_the_dockerfile_stages_the_skills_and_verifies_them_in_the_image():
    text = render.template("Dockerfile")
    stage = text.index("agent_runtime.skills.image stage")
    verify = text.index("agent_runtime.skills.image verify --app /app")
    assert text.index(" AS build") < stage < text.index(" AS runtime") < verify
    assert '--agent "/context/agents/${AGENT}" --skills /context/skills --out /stage' in text
    assert "--mount=type=bind,target=/context" in text  # no COPY of a skills/ that may be absent
    assert "COPY --from=build /stage/ /app/" in text
    ignore = render.template("dockerignore")
    assert "!skills/**" in ignore and "!agents/*/skills.lock" in ignore
