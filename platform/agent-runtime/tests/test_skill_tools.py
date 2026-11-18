"""The skill tools and what the step does with them (skills.md sections 8 and 10).
`launcher.run` is always replaced: no script is started here."""

import hashlib
import io
import json
import socket
import sys
import types

import pytest
import yaml

from agent_runtime import confine, step
from agent_runtime.skills import format as fmt
from agent_runtime.skills import launcher
from agent_runtime.skills import tools as skill_tools

SKILL_MD = "---\nname: {name}\ndescription: |-\n  {description}\n---\n\n{body}\n"
SIDECAR = "schema_version: 1\nscripts:\n  - path: scripts/count.py\n    interpreter: python3\n"

AGENT = """
from agent_runtime.tools import Done, ToolCall

def create_agent(ctx):
    def decide(task, results):
        return Done("ok") if results else ToolCall(task["tool"], task["args"])
    return {"tools": [], "decide": decide}
"""

MANIFEST = {
    "schema_version": 1,
    "name": "skilled",
    "entrypoint": "skillagent:create_agent",
    "instructions": "instructions/base.md",
    "model": None,
    "tools": [],
    "skills": ["row-count", "notes"],
}


def write_skill(app, name, description, body, scripts=False):
    directory = app / "skills" / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        SKILL_MD.format(name=name, description=description, body=body)
    )
    (directory / "references").mkdir()
    (directory / "references" / "notes.md").write_text("n" * 30000)
    (directory / "references" / "binary.bin").write_bytes(b"\xff\xfe")
    (directory / "outside.txt").write_text("outside")
    if scripts:
        (directory / "scripts").mkdir()
        (directory / "scripts" / "count.py").write_text("print(1)\n")
        (directory / "zetra.yaml").write_text(SIDECAR)


def write_lock(app, names):
    skills = [fmt.read(app / "skills" / n) for n in names]
    (app / "skills.lock").write_text(fmt.lock_text(skills))


@pytest.fixture
def app(agent_env, tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "agent.yaml").write_text(yaml.safe_dump(MANIFEST))
    (app / "instructions").mkdir()
    (app / "instructions" / "base.md").write_text("You are a test agent.")
    (app / "skillagent.py").write_text(AGENT)
    write_skill(app, "row-count", "Counts rows.\n  Use when asked how many.", "COUNT-BODY", True)
    write_skill(app, "notes", "Takes notes.", "NOTES-BODY")
    write_lock(app, ["row-count", "notes"])
    yield app
    sys.modules.pop("skillagent", None)
    while str(app) in sys.path:
        sys.path.remove(str(app))


@pytest.fixture
def boundary(app, tmp_path, monkeypatch, capsys):
    """Run one boundary in this process: returns (exit status, output record or None)."""
    monkeypatch.setattr(confine, "apply", lambda: True)
    monkeypatch.setenv("AGENT_EFFECTS", "sandbox_execute")

    def run(n, task=None):
        body = json.dumps({"boundary": n, "task": task or {"goal": "t"}}).encode()
        monkeypatch.setattr(
            sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"%d\n" % len(body) + body))
        )
        with pytest.raises(SystemExit) as exit_:
            step.main()
        text = capsys.readouterr().out
        return exit_.value.code, (json.loads(text) if text else None)

    return run


def call(boundary, tool, args):
    """The two boundaries that make one tool call: decide, then run the tool."""
    assert boundary(0, {"tool": tool, "args": args})[0] == 0
    return boundary(1, {"tool": tool, "args": args})


def saved(tmp_path):
    return json.loads((tmp_path / "sandbox" / "state" / "state.json").read_text())


def run_of(**kw):
    values = {
        "skill": "row-count",
        "script": "scripts/count.py",
        "digest": "sha256:" + "a" * 64,
        "arguments": ("t", "é"),
        "refused": None,
        "exit_status": 0,
        "limits_hit": (),
        "stdout": "3\n",
        "stderr": "",
        "duration_ms": 41,
    }
    return launcher.Run(**{**values, **kw})


def test_two_listed_skills_are_in_the_prompt_in_manifest_order(boundary, tmp_path):
    assert boundary(0, {"tool": "skill_use", "args": {"skill": "notes"}})[0] == 0
    system = saved(tmp_path)["messages"][0]["content"]
    assert system.startswith("You are a test agent.\n## Skills\n\nBefore a task")
    lines = system.splitlines()[-2:]
    assert lines == [
        "- row-count: Counts rows. Use when asked how many.",
        "- notes: Takes notes.",
    ]


def test_an_unused_skill_costs_only_its_description(boundary, tmp_path):
    boundary(0, {"tool": "skill_use", "args": {"skill": "notes"}})
    text = json.dumps(saved(tmp_path)["messages"])
    assert "COUNT-BODY" not in text and "NOTES-BODY" not in text
    rc, out = boundary(1, {"tool": "skill_use", "args": {"skill": "notes"}})
    assert rc == 0 and out["tool"] == "skill_use" and "script_run" not in out
    text = json.dumps(saved(tmp_path)["messages"])
    assert "NOTES-BODY" in text and "COUNT-BODY" not in text


def test_skill_use_result_and_nothing_is_fetched(boundary, tmp_path, monkeypatch):
    def no_network(*args, **kw):
        raise AssertionError("the network was used")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    call(boundary, "skill_use", {"skill": "row-count"})
    result = json.loads(saved(tmp_path)["messages"][-1]["content"])
    assert result == {
        "skill": "row-count",
        "version": None,
        "body": "COUNT-BODY\n",
        "files": [
            "outside.txt",
            "references/binary.bin",
            "references/notes.md",
            "scripts/count.py",
        ],
        "scripts": ["scripts/count.py"],
    }


def test_skill_use_lists_no_scripts_when_run_is_not_offered(boundary, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_EFFECTS", "workspace_read")
    call(boundary, "skill_use", {"skill": "row-count"})
    assert json.loads(saved(tmp_path)["messages"][-1]["content"])["scripts"] == []


def test_skill_file_windows_and_refuses_what_is_outside_the_skill(boundary, tmp_path):
    path = "references/notes.md"
    call(boundary, "skill_file", {"skill": "notes", "path": path})
    first = json.loads(saved(tmp_path)["messages"][-1]["content"])
    assert first["content"] == "n" * 24000 and first["next_offset"] == 24000

    tools = skill_tools.SkillTools(
        tmp_path / "app", tmp_path, [fmt.read(tmp_path / "app/skills/notes")], ""
    )

    async def ask(**kw):
        return await tools.skill_file(skill="notes", **kw)

    import asyncio

    rest = asyncio.run(ask(path=path, offset=24000))
    assert rest["content"] == "n" * 6000 and rest["next_offset"] is None
    for bad in ("../row-count/SKILL.md", "/etc/passwd", "", "references/../SKILL.md", "nope"):
        assert asyncio.run(ask(path=bad))["refused"] == "path"
    (tmp_path / "app/skills/notes/link").symlink_to("../row-count/SKILL.md")
    assert asyncio.run(ask(path="link"))["refused"] == "path"
    assert asyncio.run(ask(path="references/binary.bin"))["refused"] == "not_text"
    assert asyncio.run(ask(path=path, offset=-1))["refused"] == "arguments"
    other = asyncio.run(tools.skill_file(skill="unlisted", path=path))
    assert other["refused"] == "unknown_skill"


def test_skill_file_window_shrinks_to_fit_the_result_bound(app, tmp_path):
    (app / "skills/notes/references/wide.md").write_text("é" * 30000)
    import asyncio

    tools = skill_tools.SkillTools(app, tmp_path, [fmt.read(app / "skills/notes")], "")
    result = asyncio.run(tools.skill_file("notes", "references/wide.md"))
    assert len(json.dumps(result)) <= 60000 and 0 < len(result["content"]) < 24000
    assert result["next_offset"] == len(result["content"])


def test_skill_run_is_not_offered_without_the_effect(boundary, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_EFFECTS", "workspace_read")
    monkeypatch.setattr(launcher, "run", lambda *a, **k: pytest.fail("a script was started"))
    rc, out = boundary(0, {"tool": "skill_run", "args": {}})
    assert rc == 3 and "skill_run" in out["summary"]
    assert "skill_run" not in {
        s["function"]["name"] for s in offered(tmp_path / "app", "workspace_read")
    }
    assert "skill_run" in {
        s["function"]["name"] for s in offered(tmp_path / "app", "sandbox_execute")
    }


def offered(app, effects):
    skills = skill_tools.load(app, MANIFEST)
    built = skill_tools.SkillTools(app, app, skills, effects)
    return [step.schema(t) for t in built.tools().values()]


def test_skill_run_is_not_offered_when_no_listed_skill_has_scripts(app):
    manifest = {**MANIFEST, "skills": ["notes"]}
    write_lock(app, ["notes"])
    built = skill_tools.SkillTools(
        app, app, skill_tools.load(app, manifest), "workspace_read,sandbox_execute"
    )
    assert set(built.tools()) == {"skill_use", "skill_file"}


def test_a_call_gives_the_tool_result_and_the_script_run_key(boundary, tmp_path, monkeypatch):
    seen = []

    def fake(app, workspace, listed, skill, script, arguments, **kw):
        seen.append((app, workspace, list(listed), skill, script, arguments))
        return run_of()

    monkeypatch.setattr(launcher, "run", fake)
    args = {"skill": "row-count", "script": "scripts/count.py", "arguments": ["t", "é"]}
    rc, out = call(boundary, "skill_run", args)
    assert rc == 0 and out["tool"] == "skill_run"
    assert seen == [
        (
            tmp_path / "app",
            tmp_path / "sandbox" / "workspace",
            ["row-count", "notes"],
            "row-count",
            "scripts/count.py",
            ["t", "é"],
        )
    ]
    compact = json.dumps(["t", "é"], ensure_ascii=False, separators=(",", ":"))
    assert out["script_run"] == {
        "skill": "row-count",
        "script": "scripts/count.py",
        "digest": "sha256:" + "a" * 64,
        "argument_count": 2,
        "arguments_sha256": hashlib.sha256(compact.encode()).hexdigest(),
        "exit_status": 0,
        "limits_hit": [],
        "refused": None,
        "duration_ms": 41,
    }
    result = json.loads(saved(tmp_path)["messages"][-1]["content"])
    assert result["stdout"] == "3\n" and result["exit_status"] == 0


def test_a_refused_run_is_recorded_with_refused_set(boundary, monkeypatch):
    refused = run_of(
        skill=None, script=None, digest=None, arguments=(), refused="unknown_skill",
        exit_status=None, duration_ms=0,
    )  # fmt: skip
    monkeypatch.setattr(launcher, "run", lambda *a, **k: refused)
    rc, out = call(boundary, "skill_run", {"skill": "made-up", "script": "x", "arguments": []})
    record = out["script_run"]
    assert rc == 0 and record["refused"] == "unknown_skill"
    assert record["skill"] is None and record["script"] is None and record["digest"] is None
    assert record["argument_count"] == 0 and record["exit_status"] is None
    assert record["arguments_sha256"] == hashlib.sha256(b"[]").hexdigest()


def test_the_script_run_key_is_on_no_other_boundary(boundary):
    rc, out = call(boundary, "skill_use", {"skill": "notes"})
    assert rc == 0 and "script_run" not in out


@pytest.mark.parametrize("damage", ["missing", "stale", "unpinned", "no-lock"])
def test_a_missing_skill_or_a_stale_lock_exits_3(boundary, app, damage):
    if damage == "missing":
        (app / "skills/notes/SKILL.md").unlink()
        (app / "skills/notes/references/notes.md").unlink()
        (app / "skills/notes/references/binary.bin").unlink()
        (app / "skills/notes/references").rmdir()
        (app / "skills/notes/outside.txt").unlink()
        (app / "skills/notes").rmdir()
    elif damage == "stale":
        (app / "skills/notes/SKILL.md").write_text(
            SKILL_MD.format(name="notes", description="Changed.", body="x")
        )
    elif damage == "unpinned":
        write_lock(app, ["row-count"])
    else:
        (app / "skills.lock").unlink()
    rc, out = boundary(0)
    assert rc == 3 and out["summary"].startswith("agent error: SkillError")


def test_a_launcher_failure_exits_6_and_keeps_nothing(boundary, tmp_path, monkeypatch):
    def fail(*args, **kw):
        raise launcher.LauncherFailure("processes remain")

    monkeypatch.setattr(launcher, "run", fail)
    args = {"skill": "row-count", "script": "scripts/count.py", "arguments": []}
    assert boundary(0, {"tool": "skill_run", "args": args})[0] == 0
    rc, out = boundary(1, {"tool": "skill_run", "args": args})
    assert rc == 6 and "script_run" not in out
    assert saved(tmp_path)["next"] == 1


def test_an_agent_without_skills_is_unchanged(boundary, app, tmp_path):
    (app / "agent.yaml").write_text(yaml.safe_dump({**MANIFEST, "skills": []}))
    rc, out = boundary(0, {"tool": "skill_use", "args": {"skill": "notes"}})
    assert rc == 3 and "script_run" not in out and "skill_use" in out["summary"]
    assert saved(tmp_path)["messages"][0]["content"] == "You are a test agent."
