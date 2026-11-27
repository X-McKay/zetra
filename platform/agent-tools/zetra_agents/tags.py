"""Release tags that a merge to main still owes, and the check that a changed agent
carries a new version.

A release is a tag `<agent>/v<version>`; the tag workflow creates the ones that do not
exist yet, so the version in `agents/<name>/pyproject.toml` is what releases an agent.
Both functions take the git call as an argument so a test can fake it.
"""

from __future__ import annotations

import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path, PurePosixPath

Git = Callable[..., str]


class TagError(ValueError):
    pass


def git_in(repo: Path) -> Git:
    """The real git call for `repo`: arguments in, stdout out, TagError on failure."""

    def git(*args: str) -> str:
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise TagError(f"git {' '.join(args)}: {proc.stderr.strip()}")
        return proc.stdout

    return git


def version_of(text: str, where: str) -> str:
    try:
        version = (tomllib.loads(text).get("project") or {}).get("version")
    except tomllib.TOMLDecodeError as exc:
        raise TagError(f"{where}: not valid TOML: {exc}") from exc
    if not isinstance(version, str) or not version:
        raise TagError(f"{where}: no [project] version")
    return version


def missing_tags(repo: Path, git: Git) -> list[str]:
    """`<agent>/v<version>` for each agent whose tag does not exist, in name order."""
    existing = set(git("tag", "--list").split())
    agents = repo / "agents"
    found = []
    for pyproject in sorted(agents.glob("*/pyproject.toml")) if agents.is_dir() else []:
        name = pyproject.parent.name
        tag = f"{name}/v{version_of(pyproject.read_text(encoding='utf-8'), str(pyproject))}"
        if tag not in existing:
            found.append(tag)
    return found


def exempt(relative: PurePosixPath) -> bool:
    """Tests and documents: a change to them does not need a new version."""
    return relative.suffix == ".md" or "tests" in relative.parts[:1]


def unversioned_changes(repo: Path, base: str, git: Git) -> list[str]:
    """Problems for each agent whose source differs from `base` with its version unchanged,
    or whose new version already has its tag (the tag workflow only creates tags that do not
    exist, so that change would never be released).

    The comparison is against the merge base, so a branch that is behind `base` is not
    blamed for what `base` gained. An agent that did not exist there is new, and one whose
    whole directory is gone at HEAD was removed: there is nothing left to release.
    """
    anchor = git("merge-base", base, "HEAD").strip()
    changed = git("diff", "--name-only", "-z", anchor, "HEAD").split("\0")
    touched = set()
    for path in filter(None, changed):
        parts = PurePosixPath(path).parts
        if len(parts) > 2 and parts[0] == "agents" and not exempt(PurePosixPath(*parts[2:])):
            touched.add(parts[1])
    touched = {name for name in touched if git("ls-tree", "--name-only", "HEAD", f"agents/{name}/").strip()}
    problems = []
    existing = set(git("tag", "--list").split()) if touched else set()
    for name in sorted(touched):
        relative = f"agents/{name}/pyproject.toml"
        try:
            before = version_of(git("show", f"{anchor}:{relative}"), f"{relative} at {base}")
        except TagError:
            before = None  # no such file or version at the base: a new agent
        try:
            now = version_of(git("show", f"HEAD:{relative}"), relative)
        except TagError as exc:
            problems.append(str(exc))
            continue
        if now == before:
            problems.append(
                f"{name}: files under agents/{name}/ changed but the version in {relative} "
                f"is still {now}; raise it"
            )
        elif f"{name}/v{now}" in existing:
            problems.append(
                f"{name}: version {now} in {relative} already has the tag {name}/v{now}; "
                "raise it above the highest tag"
            )
    return problems
