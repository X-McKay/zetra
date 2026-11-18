"""The skill files of `platform/registry/contract/skills.md` sections 2 to 5: the directory
digest against the contract's cases, the sidecar, and the lock."""

import hashlib
import json
import os
import pathlib

import pytest

from agent_runtime.skills import format as fmt

CASES = json.loads(
    (
        pathlib.Path(__file__).parents[2] / "registry" / "contract" / "skill-digest-cases.json"
    ).read_text(encoding="utf-8")
)["cases"]

SKILL_MD = (
    "---\nname: {name}\ndescription: Counts rows. Use when asked how many.\n{extra}---\n\nBody.\n"
)


def make(root: pathlib.Path, name: str = "row-count", extra: str = "", **files: str):
    """A skill directory `root/name` with a valid SKILL.md and the given extra files
    (keys use `__` for `/`)."""
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL_MD.format(name=name, extra=extra))
    for key, content in files.items():
        path = skill / key.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return skill


SIDECAR = """\
schema_version: 1
requires:
  tools:
    - backend: postgres-admin
      tools: [execute_sql, list_schemas]
scripts:
  - path: scripts/count.py
    interpreter: python3
    timeout_seconds: 30
evaluation: evals/cases.yaml
"""


def with_sidecar(root: pathlib.Path, sidecar: str = SIDECAR, **more: str) -> pathlib.Path:
    files = {"zetra.yaml": sidecar, "scripts__count.py": "print(1)\n", "evals__cases.yaml": "[]\n"}
    return make(root, **{**files, **more})


# -- digest ---------------------------------------------------------------------------


def build(root: pathlib.Path, case: dict) -> pathlib.Path:
    skill = root / "skill"
    skill.mkdir()
    for entry in case["files"] + case.get("ignored", []):
        path = skill / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(entry["content"].encode("utf-8"))
        path.chmod(0o755 if entry["executable"] else 0o644)
    return skill


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_digest_matches_the_contract_case(tmp_path, case):
    assert fmt.digest(build(tmp_path, case)) == case["digest"]
    # The case states its own arithmetic: the digest is the sha256 of the lines.
    assert case["digest"] == "sha256:" + hashlib.sha256(case["lines"].encode()).hexdigest()
    assert [line.split(" ", 2)[2] for line in case["lines"].splitlines()] == [
        relative for relative, _ in fmt.files(tmp_path / "skill")
    ]


def test_cases_say_what_changes_a_digest_and_what_does_not():
    by_name = {c["name"]: c["digest"] for c in CASES}
    assert by_name["script-not-executable"] != by_name["with-script"]
    assert by_name["bytecode-cache-ignored"] == by_name["with-script"]


def test_digest_changes_with_content_name_and_mode(tmp_path):
    skill = make(tmp_path, scripts__run="a\n")
    first = fmt.digest(skill)
    (skill / "scripts" / "run").write_text("b\n")
    second = fmt.digest(skill)
    (skill / "scripts" / "run").rename(skill / "scripts" / "go")
    third = fmt.digest(skill)
    (skill / "scripts" / "go").chmod(0o755)
    assert len({first, second, third, fmt.digest(skill)}) == 4
    # Only the owner's execute bit counts, so a checkout with another umask is the same skill.
    (skill / "scripts" / "go").chmod(0o700)
    executable = fmt.digest(skill)
    (skill / "scripts" / "go").chmod(0o775)
    assert fmt.digest(skill) == executable


def test_a_symbolic_link_anywhere_is_refused(tmp_path):
    skill = make(tmp_path)
    (tmp_path / "outside").write_text("secret\n")
    (skill / "references").mkdir()
    (skill / "references" / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(fmt.SkillError, match="references/link is a symbolic link"):
        fmt.digest(skill)
    (skill / "references" / "link").unlink()
    (skill / "linked").symlink_to(skill / "references", target_is_directory=True)
    with pytest.raises(fmt.SkillError, match="linked is a symbolic link"):
        fmt.digest(skill)
    link = tmp_path / "row-count-link"
    link.symlink_to(skill, target_is_directory=True)
    with pytest.raises(fmt.SkillError, match="symbolic link"):
        fmt.digest(link)


def test_a_file_that_is_not_regular_and_a_name_with_a_line_break_are_refused(tmp_path):
    skill = make(tmp_path)
    os.mkfifo(skill / "pipe")
    with pytest.raises(fmt.SkillError, match="pipe is not a regular file"):
        fmt.digest(skill)
    (skill / "pipe").unlink()
    (skill / "a\nb").write_text("x")
    with pytest.raises(fmt.SkillError, match="line break"):
        fmt.digest(skill)


# -- SKILL.md -------------------------------------------------------------------------


def test_read_takes_name_description_and_version_as_written(tmp_path):
    skill = fmt.read(make(tmp_path, extra="metadata:\n  version: 1.10\n"))
    assert (skill.name, skill.version, skill.sidecar) == ("row-count", "1.10", None)
    assert skill.description == "Counts rows. Use when asked how many."
    assert skill.digest == fmt.digest(tmp_path / "row-count")
    assert fmt.read(make(tmp_path / "other")).version is None


def test_front_matter_returns_the_body(tmp_path):
    meta, body = fmt.front_matter(make(tmp_path))
    assert meta["name"] == "row-count" and body == "Body.\n"


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("no front matter\n", "must start with a front matter block"),
        ("---\nname: row-count\n", "not closed"),
        ("---\n- a\n---\n", "must be a mapping"),
        ("---\nname: other\ndescription: d\n---\n", "name is 'other'"),
        ("---\nname: row-count\n---\n", "no description"),
        ("---\nname: row-count\ndescription: " + "d" * 1025 + "\n---\n", "over 1024"),
        ("---\nname: row-count\ndescription: d\nmetadata:\n  version: a b\n---\n", "one word"),
    ],
)
def test_read_refuses_a_skill_file_that_breaks_the_format(tmp_path, text, match):
    skill = make(tmp_path)
    (skill / "SKILL.md").write_text(text)
    with pytest.raises(fmt.SkillError, match=match):
        fmt.read(skill)


@pytest.mark.parametrize("name", ["Row-Count", "row_count", "-row", "row--count", "r" * 65])
def test_read_refuses_a_directory_that_is_not_a_skill_name(tmp_path, name):
    with pytest.raises(fmt.SkillError, match="not a skill name"):
        fmt.read(make(tmp_path, name=name))


def test_read_refuses_a_directory_without_a_skill_file(tmp_path):
    (tmp_path / "row-count").mkdir()
    with pytest.raises(fmt.SkillError, match="cannot be read"):
        fmt.read(tmp_path / "row-count")


# -- sidecar --------------------------------------------------------------------------


def test_sidecar_states_tools_scripts_and_evaluation(tmp_path):
    sidecar = fmt.read(with_sidecar(tmp_path)).sidecar
    assert sidecar.tools == (fmt.ToolNeed("postgres-admin", ("execute_sql", "list_schemas")),)
    assert sidecar.scripts == (fmt.Script("scripts/count.py", "python3", 30),)
    assert sidecar.evaluation == "evals/cases.yaml"
    assert sidecar.script("scripts/count.py").timeout_seconds == 30
    assert sidecar.script("scripts/other.py") is None


def test_a_sidecar_may_say_only_its_version_and_a_timeout_has_a_default(tmp_path):
    assert fmt.read(with_sidecar(tmp_path, "schema_version: 1\n")).sidecar == fmt.Sidecar()
    text = "schema_version: 1\nscripts:\n  - {path: scripts/count.py, interpreter: bash}\n"
    script = fmt.read(with_sidecar(tmp_path / "b", text)).sidecar.scripts[0]
    assert script == fmt.Script("scripts/count.py", "bash", fmt.DEFAULT_TIMEOUT_SECONDS)


def _script(**entry) -> str:
    return "schema_version: 1\nscripts:\n  - " + json.dumps(
        {"path": "scripts/count.py", "interpreter": "python3", **entry}
    )


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[]\n", "must be a mapping"),
        ("schema_version: 2\n", "schema_version must be 1"),
        ("schema_version: true\n", "schema_version must be 1"),
        ("schema_version: 1\nallowed-tools: [x]\n", "unknown key 'allowed-tools'"),
        ("schema_version: 1\nrequires: {effects: [x]}\n", "unknown key 'effects'"),
        ("schema_version: 1\nrequires: {tools: [{backend: B, tools: [x]}]}\n", "backend name"),
        ("schema_version: 1\nrequires: {tools: [{backend: b, tools: []}]}\n", "at least one"),
        (
            "schema_version: 1\nrequires: {tools: [{backend: b, tools: [x]}, {backend: b, tools: [y]}]}\n",
            "more than once",
        ),
        (_script(path="scripts/missing.py"), "not a file of the skill"),
        (_script(path="../count.py"), "must be under scripts/"),
        (_script(path="/etc/passwd"), "must be under scripts/"),
        (_script(path=7), "must be under scripts/"),
        (_script(path="scripts/" + "a" * 121), "at most 128"),
        (_script(path="scripts/./count.py"), "plain relative path"),
        (_script(path="scripts/../scripts/count.py"), "plain relative path"),
        (_script(path="evals/cases.yaml"), "must be under scripts/"),
        (_script(path="scripts/co unt.py"), "must be under scripts/"),
        (_script(interpreter="/usr/bin/python3"), "command name"),
        (_script(interpreter="python3 -c"), "command name"),
        (_script(interpreter=""), "command name"),
        (_script(timeout_seconds=0), "from 1 to 240"),
        (_script(timeout_seconds=241), "from 1 to 240"),
        (_script(timeout_seconds="60"), "from 1 to 240"),
        (_script(shell=True), "unknown key 'shell'"),
        (_script() + "\n  - " + _script().split("- ", 1)[1], "more than once"),
        ("schema_version: 1\nevaluation: ../x\n", "plain relative path"),
        ("schema_version: 1\nevaluation: evals/none.yaml\n", "not a file of the skill"),
        ("schema_version: [\n", "not YAML text"),
    ],
)
def test_sidecar_refuses_what_the_contract_does_not_define(tmp_path, text, match):
    with pytest.raises(fmt.SkillError, match=match):
        fmt.read(with_sidecar(tmp_path, text))


# -- lock -----------------------------------------------------------------------------


def test_lock_text_is_sorted_stable_and_read_back(tmp_path):
    b = fmt.read(make(tmp_path, name="b-skill", extra="metadata:\n  version: '2'\n"))
    a = fmt.read(make(tmp_path, name="a-skill"))
    text = fmt.lock_text([b, a])
    assert text == fmt.lock_text([a, b]) and text.endswith("}\n")
    assert json.loads(text) == {
        "schema_version": 1,
        "skills": [
            {"digest": a.digest, "name": "a-skill"},
            {"digest": b.digest, "name": "b-skill", "version": "2"},
        ],
    }
    lock = tmp_path / "skills.lock"
    lock.write_text(text)
    assert fmt.read_lock(lock) == {
        "a-skill": {"name": "a-skill", "digest": a.digest},
        "b-skill": {"name": "b-skill", "digest": b.digest, "version": "2"},
    }


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"schema_version": 1}',
        '{"schema_version": 2, "skills": []}',
        '{"schema_version": 1, "skills": [{"name": "a"}]}',
        '{"schema_version": 1, "skills": [{"name": "a", "digest": "sha256:00"}]}',
        '{"schema_version": 1, "skills": [{"name": "a", "digest": "sha256:%s", "path": "x"}]}'
        % ("0" * 64),
        '{"schema_version": 1, "skills": [%s, %s]}'
        % (('{"name": "a", "digest": "sha256:%s"}' % ("0" * 64),) * 2),
    ],
)
def test_read_lock_refuses_anything_but_pinned_skills(tmp_path, text):
    lock = tmp_path / "skills.lock"
    lock.write_text(text)
    with pytest.raises(fmt.SkillError):
        fmt.read_lock(lock)


def test_lock_problems_names_what_is_stale_missing_or_extra(tmp_path):
    skills, lock = tmp_path / "skills", tmp_path / "skills.lock"
    make(skills, name="a-skill")
    make(skills, name="b-skill")
    assert fmt.lock_problems([], skills, lock) == []
    assert fmt.lock_problems(["a-skill"], skills, lock) == ["skills.lock is missing"]

    lock.write_text(fmt.lock_text([fmt.read(skills / "a-skill"), fmt.read(skills / "b-skill")]))
    assert fmt.lock_problems(["a-skill", "b-skill"], skills, lock) == []
    assert fmt.lock_problems(["a-skill"], skills, lock) == [
        "skills.lock pins b-skill, which the agent does not list"
    ]
    assert fmt.lock_problems([], skills, lock) == [
        "skills.lock exists and the agent lists no skills"
    ]

    (skills / "a-skill" / "notes.md").write_text("new\n")
    assert fmt.lock_problems(["a-skill", "b-skill"], skills, lock) == [
        "skill a-skill changed since it was pinned in skills.lock"
    ]
    lock.write_text(fmt.lock_text([fmt.read(skills / "b-skill")]))
    assert fmt.lock_problems(["a-skill", "b-skill"], skills, lock) == [
        "skill a-skill is not pinned in skills.lock"
    ]
    assert fmt.lock_problems(["a-skill", "c-skill"], skills, lock) == [
        "c-skill: the skill directory is missing or is a symbolic link"
    ]


# -- release entry --------------------------------------------------------------------


def test_release_entry_says_what_the_release_carries(tmp_path):
    undeclared = fmt.read(make(tmp_path, name="plain"))
    assert fmt.release_entry(undeclared) == {
        "name": "plain",
        "digest": undeclared.digest,
        "description": "Counts rows. Use when asked how many.",
        "declared": False,
    }
    declared = fmt.read(with_sidecar(tmp_path / "x"))
    assert fmt.release_entry(declared) == {
        "name": "row-count",
        "digest": declared.digest,
        "description": "Counts rows. Use when asked how many.",
        "declared": True,
        "scripts": ["scripts/count.py"],
        "interpreters": ["python3"],
        "tools": [{"backend": "postgres-admin", "tools": ["execute_sql", "list_schemas"]}],
    }


# --- the list in the manifest


@pytest.mark.parametrize("manifest", [{}, {"skills": None}, {"skills": []}, None])
def test_a_manifest_without_skills_lists_none(manifest):
    assert fmt.listed(manifest) == []


def test_the_listed_names_keep_the_manifests_order():
    assert fmt.listed({"skills": ["row-count", "alpha"]}) == ["row-count", "alpha"]


@pytest.mark.parametrize(
    ("skills", "says"),
    [
        ("row-count", "must be a list"),
        (["Row-Count"], "must be a list of skill names"),
        (["../x"], "must be a list of skill names"),
        ([1], "must be a list of skill names"),
        (["a", "a"], "more than once"),
        ([f"s{n}" for n in range(17)], "more than 16"),
    ],
)
def test_a_list_that_is_not_skill_names_is_refused(skills, says):
    with pytest.raises(fmt.SkillError, match=says):
        fmt.listed({"skills": skills})
