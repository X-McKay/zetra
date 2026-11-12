"""`.github/actions/platform-release/release.sh` with a fake `docker` on PATH.

The refusals, the tag, the overwrite check and the build invocation are all decisions in the
script, so they are exercised here against throwaway git repositories (a bare "origin" and a
work clone) and a fake docker that records its arguments and behaves as the env says.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPT = REPO / ".github" / "actions" / "platform-release" / "release.sh"
IMAGES_PY = REPO / "platform" / "contracts" / "agent_platform" / "images.py"
DIGEST = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32
SECRET = "c2VjcmV0LXZhbHVl"  # the fake credential in the client configuration

ENTRIES = [
    {
        "repository": "plain",
        "dockerfile": "platform/plain/Dockerfile",
        "context": "platform/plain",
        "platform": "linux/amd64",
        "buildArgs": {},
    },
    {
        "repository": "withargs",
        "dockerfile": "platform/withargs/Dockerfile",
        "context": "platform",
        "platform": "linux/arm64",
        "buildArgs": {"A": "1", "B": "two words"},
    },
]

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
state=$FAKE_STATE
if [[ $1 == buildx && $2 == imagetools ]]; then
  if [[ -n ${FAKE_INSPECT_ERROR:-} ]]; then echo "$FAKE_INSPECT_ERROR" >&2; exit 1; fi
  if [[ -e $state/built ]]; then echo "${FAKE_READBACK:-$FAKE_DIGEST}"; exit 0; fi
  if [[ -n ${FAKE_EXISTING:-} ]]; then echo "$FAKE_EXISTING"; exit 0; fi
  echo "ERROR: $3: not found" >&2; exit 1
fi
if [[ $1 == buildx && $2 == build ]]; then
  while (($#)); do
    [[ $1 == --metadata-file ]] && meta=$2
    shift
  done
  touch "$state/built"
  [[ -n ${FAKE_BUILD_FAILS:-} ]] && exit 1
  printf '{"containerimage.digest": "%s"}' "$FAKE_DIGEST" > "$meta"
  exit 0
fi
echo "unexpected docker call: $*" >&2; exit 99
"""


def run(*args: str, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


class Rig:
    def __init__(self, root: Path):
        self.root = root
        self.origin = root / "origin.git"
        self.work = root / "work"
        self.bin = root / "bin"
        self.state = root / "state"
        self.log = root / "docker.log"
        self.config = root / "dockerconfig"
        for d in (self.bin, self.state, self.config):
            d.mkdir()
        fake = self.bin / "docker"
        fake.write_text(FAKE_DOCKER)
        fake.chmod(0o755)
        self.work.mkdir()
        self.git("init", "-q", "-b", "main")
        for k, v in (("user.email", "t@example.invalid"), ("user.name", "t")):
            self.git("config", k, v)
        (self.work / "platform").mkdir()
        (self.work / "platform" / "images.json").write_text(json.dumps(ENTRIES))
        self.commit("base")
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.work), str(self.origin)], check=True
        )
        self.git("remote", "add", "origin", str(self.origin))
        (self.config / "config.json").write_text(
            json.dumps({"auths": {"registry.almckay.io": {"auth": SECRET}}})
        )

    def git(self, *args: str, cwd: Path | None = None) -> str:
        done = run("git", *args, cwd=cwd or self.work)
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    def commit(self, message: str = "c") -> str:
        (self.work / f"f-{message}").write_text(message)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def push_main(self) -> None:
        self.git("push", "-q", "origin", "main")

    def env(self, sha: str, **extra: str) -> dict:
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.root),
            "GITHUB_SHA": sha,
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": "refs/heads/main",
            "DOCKER_CONFIG": str(self.config),
            "PLATFORM_PYTHON": sys.executable,
            "IMAGES_PY": str(IMAGES_PY),
            "FAKE_LOG": str(self.log),
            "FAKE_STATE": str(self.state),
            "FAKE_DIGEST": DIGEST,
            "GITHUB_OUTPUT": str(self.root / "output"),
            "GITHUB_STEP_SUMMARY": str(self.root / "summary"),
        }
        env.update(extra)
        return env

    def script(self, *args: str, sha: str | None = None, cwd: Path | None = None, **extra: str):
        sha = sha or self.git("rev-parse", "HEAD")
        return run(str(SCRIPT), *args, cwd=cwd or self.work, env=self.env(sha, **extra))

    def docker_calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_script_is_executable_and_shellcheck_clean_where_available():
    assert os.access(SCRIPT, os.X_OK)
    shellcheck = subprocess.run(["which", "shellcheck"], capture_output=True)
    if shellcheck.returncode == 0:
        done = run("shellcheck", "-x", str(SCRIPT), cwd=REPO)
        assert done.returncode == 0, done.stdout


def test_tag_is_the_first_seven_characters_of_the_commit(rig):
    done = rig.script("tag")
    assert done.returncode == 0 and done.stdout.strip() == rig.git("rev-parse", "HEAD")[:7]


@pytest.mark.parametrize("sha", ["abc", "G" * 40, "a" * 39, "a" * 41, ""])
def test_tag_refuses_anything_but_a_full_hash(rig, sha):
    assert rig.script("tag", sha=sha, GITHUB_SHA=sha).returncode != 0


def test_config_missing_names_the_step_and_prints_nothing_secret(rig):
    (rig.config / "config.json").unlink()
    done = rig.script("config")
    assert done.returncode != 0
    assert "registry.almckay.io" in done.stderr
    assert "arc-runners README of the infrastructure repository" in done.stderr
    assert SECRET not in done.stdout + done.stderr


@pytest.mark.parametrize(
    "content", ["not json", "{}", '{"auths": {"other.example": {"auth": "x"}}}', '{"auths": []}']
)
def test_config_without_this_registry_is_refused(rig, content):
    (rig.config / "config.json").write_text(content)
    assert rig.script("config").returncode != 0


def test_config_present_passes_and_stays_quiet(rig):
    done = rig.script("config")
    assert done.returncode == 0
    assert SECRET not in done.stdout + done.stderr


def test_preflight_accepts_the_pushed_commit_on_main(rig):
    rig.push_main()
    assert rig.script("preflight", "plain").returncode == 0


def test_preflight_works_from_a_shallow_checkout(rig, tmp_path):
    rig.push_main()
    tip = rig.commit("later")
    rig.push_main()
    shallow = tmp_path / "shallow"
    run("git", "clone", "-q", "--depth", "1", f"file://{rig.origin}", str(shallow), cwd=tmp_path)
    first = rig.git("rev-list", "--max-parents=0", "HEAD")
    # The shallow clone holds only the tip, but the pushed commit is the tip: ancestry of main
    # is decided after the fetch deepens the clone.
    assert run("git", "rev-parse", "--is-shallow-repository", cwd=shallow).stdout.strip() == "true"
    assert rig.script("preflight", "plain", sha=tip, cwd=shallow).returncode == 0
    # A commit older than the tip is also an ancestor, and the checkout is not the pushed commit.
    done = rig.script("preflight", "plain", sha=first, cwd=shallow)
    assert done.returncode != 0 and "not the pushed commit" in done.stderr


def test_preflight_refuses_an_image_that_is_not_an_entry(rig):
    rig.push_main()
    done = rig.script("preflight", "nope")
    assert done.returncode != 0 and "not an entry" in done.stderr
    assert rig.script("preflight", "../plain").returncode != 0


def test_preflight_refuses_a_checkout_that_is_not_the_pushed_commit(rig):
    rig.push_main()
    done = rig.script("preflight", "plain", sha="1" * 40)
    assert done.returncode != 0 and "not the pushed commit" in done.stderr


def test_preflight_refuses_a_commit_that_is_not_on_main(rig):
    rig.push_main()
    rig.git("checkout", "-q", "-b", "side")
    rig.commit("side")
    done = rig.script("preflight", "plain")
    assert done.returncode != 0 and "not an ancestor of origin/main" in done.stderr


@pytest.mark.parametrize("name", ["new.txt", "platform/stray"])
def test_preflight_refuses_untracked_files(rig, name):
    rig.push_main()
    (rig.work / name).write_text("x")
    done = rig.script("preflight", "plain")
    assert done.returncode != 0 and "uncommitted or untracked" in done.stderr


def test_preflight_refuses_modified_tracked_files(rig):
    rig.push_main()
    (rig.work / "f-base").write_text("changed")
    assert rig.script("preflight", "plain").returncode != 0


def test_preflight_refuses_other_events_and_refs(rig):
    rig.push_main()
    assert rig.script("preflight", "plain", GITHUB_EVENT_NAME="workflow_dispatch").returncode != 0
    assert rig.script("preflight", "plain", GITHUB_REF="refs/heads/other").returncode != 0
    assert rig.script("preflight", "plain", GITHUB_REF="refs/tags/v1").returncode != 0


def test_push_builds_with_the_entry_and_reports_the_digest(rig):
    sha = rig.git("rev-parse", "HEAD")
    done = rig.script("push", "withargs")
    assert done.returncode == 0, done.stderr
    ref = f"registry.almckay.io/platform/withargs:{sha[:7]}"
    build = [c for c in rig.docker_calls() if c.startswith("buildx build")]
    assert len(build) == 1
    assert build[0].startswith("buildx build --pull --no-cache --platform linux/arm64 ")
    assert "--build-arg A=1 --build-arg B=two words" in build[0]
    assert "-f platform/withargs/Dockerfile" in build[0]
    assert f"-t {ref} --push --metadata-file " in build[0]
    assert build[0].endswith(" platform")
    full = f"{ref}@{DIGEST}"
    assert full in done.stdout
    assert f"reference={full}" in (rig.root / "output").read_text()
    assert f"digest={DIGEST}" in (rig.root / "output").read_text()
    assert f"tag={sha[:7]}" in (rig.root / "output").read_text()
    assert full in (rig.root / "summary").read_text()


def test_push_builds_an_image_without_build_arguments(rig):
    assert rig.script("push", "plain").returncode == 0
    (build,) = [c for c in rig.docker_calls() if c.startswith("buildx build")]
    assert "--build-arg" not in build and build.endswith(" platform/plain")


def test_push_refuses_to_overwrite_an_existing_tag_and_builds_nothing(rig):
    done = rig.script("push", "plain", FAKE_EXISTING=OTHER)
    assert done.returncode != 0
    assert "already exists" in done.stderr and OTHER in done.stderr
    assert "refusing to overwrite" in done.stderr
    assert not any(c.startswith("buildx build") for c in rig.docker_calls())


def test_push_stops_when_the_registry_cannot_be_asked(rig):
    done = rig.script("push", "plain", FAKE_INSPECT_ERROR="unauthorized: authentication required")
    assert done.returncode != 0 and "could not inspect" in done.stderr
    assert not any(c.startswith("buildx build") for c in rig.docker_calls())


def test_push_fails_if_the_tag_resolves_to_another_digest_afterwards(rig):
    done = rig.script("push", "plain", FAKE_READBACK=OTHER)
    assert done.returncode != 0 and "tag changed during the push" in done.stderr
    assert not (rig.root / "output").exists()


def test_push_fails_when_the_build_fails(rig):
    done = rig.script("push", "plain", FAKE_BUILD_FAILS="1")
    assert done.returncode != 0
    assert not (rig.root / "output").exists()


def test_push_refuses_an_unknown_image(rig):
    done = rig.script("push", "nope")
    assert done.returncode != 0
    assert not any(c.startswith("buildx build") for c in rig.docker_calls())


def test_all_runs_every_refusal_before_building(rig):
    rig.push_main()
    (rig.config / "config.json").unlink()
    assert rig.script("all", "plain").returncode != 0
    assert not any(c.startswith("buildx build") for c in rig.docker_calls())
    (rig.config / "config.json").write_text(
        json.dumps({"auths": {"registry.almckay.io": {"auth": SECRET}}})
    )
    done = rig.script("all", "plain")
    assert done.returncode == 0, done.stderr
    assert any(c.startswith("buildx build") for c in rig.docker_calls())
