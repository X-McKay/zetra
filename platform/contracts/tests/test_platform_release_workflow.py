"""The platform release workflow and composite action, as configuration (nothing is run)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
ACTIONS = REPO / ".github" / "actions"
RELEASE = yaml.safe_load((WORKFLOWS / "platform-release.yml").read_text())
ACTION = yaml.safe_load((ACTIONS / "platform-release" / "action.yml").read_text())
AGENT_ACTION = yaml.safe_load((ACTIONS / "agent-release" / "action.yml").read_text())
HOSTED = {"ubuntu-latest"}


def triggers(workflow: dict) -> dict:
    return workflow[True] if True in workflow else workflow["on"]  # YAML 1.1 reads `on` as True


def all_uses() -> list[tuple[str, str]]:
    found = []
    for name, doc in (("workflow", RELEASE), ("action", ACTION)):
        jobs = doc["jobs"].values() if "jobs" in doc else [doc["runs"]]
        for job in jobs:
            found += [(name, step["uses"]) for step in job["steps"] if "uses" in step]
    return found


def test_the_only_trigger_is_a_push_to_main():
    # The runner's guard admits exactly: event push, ref refs/heads/main, this workflow file.
    assert triggers(RELEASE) == {"push": {"branches": ["main"]}}


def test_permissions_are_read_only_plus_the_identity_token():
    assert RELEASE["permissions"] == {"contents": "read", "id-token": "write"}
    for job in RELEASE["jobs"].values():
        assert "permissions" not in job  # no job widens them


def test_only_the_release_job_names_the_scale_set_and_it_names_it_exactly():
    jobs = RELEASE["jobs"]
    assert set(jobs) == {"changes", "release"}
    assert jobs["release"]["runs-on"] == "kubani-zetra"
    for name, job in jobs.items():
        if name != "release":
            assert job["runs-on"] in HOSTED, name
    assert str(RELEASE).count("kubani-zetra") == 1


def test_images_are_released_one_at_a_time_without_a_concurrency_group():
    strategy = RELEASE["jobs"]["release"]["strategy"]
    assert strategy["max-parallel"] == 1
    assert strategy["fail-fast"] is False
    assert "concurrency" not in RELEASE
    assert all("concurrency" not in job for job in RELEASE["jobs"].values())
    assert RELEASE["jobs"]["release"]["if"] == "needs.changes.outputs.count != '0'"


def test_every_use_is_pinned_by_commit_hash_or_is_a_local_path():
    uses = all_uses()
    assert uses
    for where, ref in uses:
        assert ref.startswith("./") or re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), (where, ref)


def test_no_registry_secret_or_login_anywhere():
    for path in (
        WORKFLOWS / "platform-release.yml",
        ACTIONS / "platform-release" / "action.yml",
        ACTIONS / "platform-release" / "release.sh",
    ):
        code = "\n".join(
            line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
        )
        assert "secrets." not in code, path
        assert "docker login" not in code, path
        assert "--password" not in code, path


def step_named(action: dict, prefix: str) -> dict:
    (step,) = [s for s in action["runs"]["steps"] if s.get("name", "").startswith(prefix)]
    return step


def test_tool_install_step_is_a_copy_of_the_agent_release_step():
    ours = step_named(ACTION, "Install cosign")
    theirs = step_named(AGENT_ACTION, "Install cosign")
    assert ours["run"] == theirs["run"]
    # DOCKER_CONFIG differs on purpose: the agent action logs in under runner.temp, here the
    # cluster mounts the configuration in the runner's default location.
    assert {k: v for k, v in theirs["env"].items() if k != "DOCKER_CONFIG"} == ours["env"]
    assert ours["shell"] == theirs["shell"]


def test_setup_uv_pin_matches_the_agent_action():
    ours = ACTION["runs"]["steps"][0]
    theirs = AGENT_ACTION["runs"]["steps"][0]
    assert ours == theirs | {"with": ours["with"]}
    assert ours["with"] == theirs["with"]


def build_line(text: str) -> str:
    (line,) = re.findall(r"docker buildx build --pull --no-cache --platform \S+", text)
    return line


def test_release_build_keeps_the_flags_of_the_pull_request_job():
    pr = (WORKFLOWS / "platform.yml").read_text()
    script = (ACTIONS / "platform-release" / "release.sh").read_text()
    assert build_line(pr).replace('"$PLATFORM"', "P") == build_line(script).replace(
        '"$platform"', "P"
    )
    assert '-f "$DOCKERFILE" "$CONTEXT"' in pr
    assert '-f "$dockerfile"' in script and '"$context"' in script
    assert "--build-arg" in pr and "--build-arg" in script
