"""Rendered repository files and new repositories.

The files that are the same for every agent repository (the Dockerfile, `.dockerignore`,
the justfile, the root `pyproject.toml`, CI and the release shim) are rendered from the
templates packaged here and verified by `zetra-agents render --check`, the way Kubani's
`check-sync` verifies its copy of each Agent. Hand edits fail the check; a change to the
shape of an agent repository is a change to these templates in zetra.

Every rendered file pins one zetra commit (`@@ZETRA_REV@@`): the sandbox SDK baked into
the images, this tooling, the contracts the Registry Controller runs, and the composite
release action. `render --check` also requires that commit to be the one this tooling was
installed from, when that is known.
"""

from __future__ import annotations

import json
import keyword
import re
import shlex
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from importlib import metadata, resources
from pathlib import Path

from zetra_agents import layout, manifest

ZETRA_GIT = "https://github.com/X-McKay/zetra.git"
ZETRA_ORIGIN = re.compile(
    r"^(?:git\+)?(?:https://|ssh://git@|git@)github\.com[:/]X-McKay/zetra(?:\.git)?/?$"
)
REV = re.compile(r"^[0-9a-f]{40}\Z")
# What a repository pins from zetra and the lock entry each must have at the pinned commit.
PINNED = {
    "agent-runtime": "platform/agent-runtime",
    "zetra-agents": "platform/agent-tools",
    "agent-platform-contracts": "platform/contracts",
}
OWNER = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\Z")
# Rendered into TOML and YAML strings and a Python docstring unescaped, so only plain text.
DESCRIPTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ,.;()/_+-]{0,199}\Z")
RESERVED_PACKAGES = {
    "agent_runtime",
    "agent_platform",
    "zetra",
    "zetra_agents",
    "agents",
    "tests",
    "src",
    "mcp",
    "openai",
    "httpx",
    "yaml",
    "pydantic",
    "pytest",
}
PLACEHOLDER = re.compile(r"@@([A-Z_]+)@@")
# published path -> template name (templates carry a .tmpl suffix so no tool mistakes them
# for real project files)
RENDERED = {
    "Dockerfile": "Dockerfile",
    ".dockerignore": "dockerignore",
    "justfile": "justfile",
    "pyproject.toml": "pyproject.toml",
    ".github/workflows/ci.yml": "workflows/ci.yml",
    ".github/workflows/release.yml": "workflows/release.yml",
}
AGENT_TEMPLATES = "agent"
PACKAGE_DIR = "__package__"
# Seeded on init and then owned by the repository: never checked.
SEEDED = {"AGENTS.md": "AGENTS.md", ".gitignore": "gitignore", "README.md": "README.md"}


class RenderError(ValueError):
    pass


@dataclass(frozen=True)
class Difference:
    path: str
    state: str  # "missing" | "differs" | "revision" | "lock" | "sources" | "tools"
    detail: str = ""


def template(name: str) -> str:
    return (resources.files("zetra_agents") / "templates" / f"{name}.tmpl").read_text(
        encoding="utf-8"
    )


def render_text(text: str, variables: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise RenderError(f"template has no value for @@{key}@@")
        return variables[key]

    return PLACEHOLDER.sub(replace, text)


def installed_revision() -> str | None:
    """The zetra commit this tooling was installed from, if it came from git.

    Either this distribution was installed from the zetra repository directly, or the
    `zetra` CLI was and brought this package along as a path dependency of that checkout;
    the first distribution that records a zetra git commit answers.
    """
    for name in ("zetra-agents", "zetra"):
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        raw = dist.read_text("direct_url.json")
        if not raw:
            continue
        try:
            info = json.loads(raw)
        except ValueError:
            continue
        if not ZETRA_ORIGIN.match(str(info.get("url", ""))):
            continue
        commit = (info.get("vcs_info") or {}).get("commit_id")
        if isinstance(commit, str) and REV.match(commit):
            return commit
    return None


def repository_revision(repo: Path) -> str | None:
    """The zetra commit the repository's root pyproject pins for agent-runtime."""
    path = repo / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    source = ((data.get("tool") or {}).get("uv") or {}).get("sources") or {}
    rev = (source.get("agent-runtime") or {}).get("rev")
    return rev if isinstance(rev, str) and REV.match(rev) else None


def resolve_revision(repo: Path | None, explicit: str | None) -> str:
    rev = explicit or (repository_revision(repo) if repo else None) or installed_revision()
    if not rev:
        raise RenderError(
            "no zetra revision: pass --zetra-rev, or run inside a repository whose root "
            "pyproject pins agent-runtime, or install zetra-agents from git"
        )
    if not REV.match(rev):
        raise RenderError("zetra revision must be a 40-hex commit")
    return rev


def rendered_files(revision: str) -> dict[str, str]:
    variables = {"ZETRA_REV": revision}
    return {path: render_text(template(name), variables) for path, name in RENDERED.items()}


def write(repo: Path, revision: str) -> list[Path]:
    written = []
    for relative, text in rendered_files(revision).items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def check(repo: Path) -> list[Difference]:
    """Every rendered file that is missing or differs from its template at the pinned commit,
    plus a lock whose zetra entries are not that commit and members that override sources."""
    revision = repository_revision(repo)
    if revision is None:
        return [Difference("pyproject.toml", "revision")]
    differences = []
    installed = installed_revision()
    if installed is not None and installed != revision:
        differences.append(Difference("pyproject.toml", "revision"))
    for relative, text in rendered_files(revision).items():
        path = repo / relative
        if not path.is_file():
            differences.append(Difference(relative, "missing"))
        elif path.read_text(encoding="utf-8") != text:
            differences.append(Difference(relative, "differs"))
    differences += lock_problems(repo, revision)
    differences += member_source_problems(repo)
    differences += tool_problems(repo)
    return differences


def derived_agents(repo: Path) -> list[layout.AgentDir]:
    """The agents whose tool list is read from their code. A conformance fixture's factory
    refuses to run outside its own image, where the Dockerfile's fixture target and the CI
    image check hold it to its manifest; an incomplete directory is `check`'s to report."""
    return [
        agent
        for agent in layout.all_agents(repo)
        if layout.image_target(agent) == "runtime" and not layout.check_files(agent)
    ]


def tool_problems(repo: Path) -> list[Difference]:
    """Manifests whose `tools:` list is not what the agent's code declares."""
    problems = []
    for agent in derived_agents(repo):
        path = agent.manifest_path.relative_to(repo).as_posix()
        try:
            detail = manifest.difference(agent)
        except manifest.ToolsError as exc:
            problems.append(Difference(path, "tools", str(exc)))
            continue
        if detail is not None:
            problems.append(Difference(path, "tools", f"{detail}; run `zetra-agents render`"))
    return problems


def write_tools(repo: Path) -> list[Path]:
    """Rewrite each manifest's `tools:` from its code; the manifests that changed."""
    return [agent.manifest_path for agent in derived_agents(repo) if manifest.write(agent)]


def lock_problems(repo: Path, revision: str) -> list[Difference]:
    """The lock must take every pinned zetra package from X-McKay/zetra at the pinned commit."""
    path = repo / "uv.lock"
    if not path.is_file():
        return [Difference("uv.lock", "lock", "missing; run `uv lock`")]
    try:
        lock = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return [Difference("uv.lock", "lock", f"not valid TOML: {exc}")]
    found = {p.get("name"): p for p in lock.get("package", []) if isinstance(p, dict)}
    problems = []
    for name, subdirectory in PINNED.items():
        want = (
            f"{ZETRA_GIT}?subdirectory={subdirectory.replace('/', '%2F')}&rev={revision}#{revision}"
        )
        entry = found.get(name)
        if entry is None:
            problems.append(Difference("uv.lock", "lock", f"{name} is not locked; run `uv lock`"))
            continue
        got = (entry.get("source") or {}).get("git")
        if got != want:
            problems.append(
                Difference(
                    "uv.lock", "lock", f"{name} is locked from {got!r}, not zetra {revision[:12]}"
                )
            )
    return problems


def member_source_problems(repo: Path) -> list[Difference]:
    """Members may not redirect any dependency; the root pyproject owns every source."""
    problems = []
    for agent in layout.all_agents(repo):
        pyproject = agent.root / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue  # layout.check reports the file itself
        if ((data.get("tool") or {}).get("uv") or {}).get("sources"):
            problems.append(
                Difference(
                    f"agents/{agent.name}/pyproject.toml",
                    "sources",
                    "[tool.uv.sources] in a member overrides the pinned zetra commit",
                )
            )
    return problems


def describe(difference: Difference, repo: Path) -> str:
    if difference.state in ("lock", "sources", "tools"):
        return f"{difference.path}: {difference.detail}"
    if difference.state == "revision":
        installed = installed_revision()
        pinned = repository_revision(repo)
        if pinned is None:
            return (
                "pyproject.toml: no zetra commit pinned for agent-runtime under [tool.uv.sources]"
            )
        return (
            f"pyproject.toml: pins zetra {pinned[:12]} but this tooling is from "
            f"{(installed or '?')[:12]}; render with the tooling from the pinned commit"
        )
    return f"{difference.path}: {difference.state}; run `zetra-agents render`"


def init(
    target: Path,
    *,
    agent: str,
    revision: str,
    tenant: str,
    owner: str,
    description: str,
) -> list[Path]:
    """Render a new repository holding one agent. The target must not exist or be empty."""
    if not layout.NAME.match(agent):
        raise RenderError("agent name must be a DNS label of at most 32 characters")
    package = agent.replace("-", "_")
    if not package.isidentifier() or keyword.iskeyword(package) or package in RESERVED_PACKAGES:
        raise RenderError(f"{agent!r} does not give a usable Python package name ({package!r})")
    if not OWNER.match(owner):
        raise RenderError("owner must be an email address")
    if not DESCRIPTION.match(description):
        raise RenderError(
            "description must be plain text (letters, digits, spaces and , . ; ( ) / _ + -), "
            "at most 200 characters"
        )
    if target.exists() and any(target.iterdir()):
        raise RenderError(f"{target} is not empty")
    variables = {
        "ZETRA_REV": revision,
        "AGENT": agent,
        "PACKAGE": package,
        "TENANT": tenant,
        "OWNER": owner,
        "DESCRIPTION": description,
    }
    target.mkdir(parents=True, exist_ok=True)
    written = write(target, revision)
    for relative, name in SEEDED.items():
        path = target / relative
        path.write_text(render_text(template(name), variables), encoding="utf-8")
        written.append(path)
    root = resources.files("zetra_agents") / "templates" / AGENT_TEMPLATES
    for entry in _walk(root):
        relative = Path(*[package if part == PACKAGE_DIR else part for part in entry.parts])
        if relative.suffix != ".tmpl":
            raise RenderError(f"agent template {entry} lacks the .tmpl suffix")
        path = target / "agents" / agent / relative.with_suffix("")
        path.parent.mkdir(parents=True, exist_ok=True)
        text = (root / entry).read_text(encoding="utf-8")
        path.write_text(render_text(text, variables), encoding="utf-8")
        written.append(path)
    return written


def _walk(root, prefix: Path = Path()) -> list[Path]:
    found = []
    for child in sorted(root.iterdir(), key=lambda c: c.name):
        if child.is_dir():
            found += _walk(child, prefix / child.name)
        else:
            found.append(prefix / child.name)
    return found


def bootstrap(target: Path, *, lock: bool = True, git: bool = True) -> dict[str, str]:
    """After rendering: lock the workspace and start a git history, when the tools exist.

    Neither step is required for the repository to be valid; both are what a person would
    do next by hand. Failures are reported, not raised.
    """
    result = {"lock": "skipped", "git": "skipped"}
    if lock:
        uv = shutil.which("uv")
        if uv is None:
            result["lock"] = "uv not found; run `uv lock`"
        else:
            try:
                proc = subprocess.run(
                    [uv, "lock", "--project", str(target)],
                    cwd=target,
                    capture_output=True,
                    text=True,
                    timeout=900,
                    check=False,
                )
                lines = [
                    ln for ln in (proc.stderr or proc.stdout).strip().splitlines() if ln.strip()
                ]
                reason = next(
                    (ln for ln in lines if ln.lstrip().startswith("error")),
                    lines[-1] if lines else "",
                )
                result["lock"] = (
                    "locked"
                    if proc.returncode == 0
                    else f"failed: {reason.strip()[:300]}; run `uv lock`"
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                result["lock"] = f"failed: {exc}; run `uv lock`"
    if git:
        g = shutil.which("git")
        if g is None:
            result["git"] = "git not found"
        else:
            inside = subprocess.run(
                [g, "-C", str(target), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                check=False,
            )
            if inside.returncode == 0:
                result["git"] = "already inside a repository"
            else:
                try:
                    subprocess.run(
                        [g, "init", "-q", "-b", "main", str(target)],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=True,
                    )
                    result["git"] = "initialised on main; nothing committed yet"
                except subprocess.CalledProcessError as exc:
                    result["git"] = f"failed: {(exc.stderr or str(exc)).strip()[:300]}"
                except (subprocess.TimeoutExpired, OSError) as exc:
                    result["git"] = f"failed: {exc}"
    return result


def next_steps(target: Path, agent: str, tenant: str, bootstrapped: dict[str, str]) -> str:
    """The checklist from a rendered repository to a deployed agent."""
    steps = [f"cd {shlex.quote(str(target))}"]
    if bootstrapped.get("lock") != "locked":
        steps.append("uv lock")
    steps.append("just setup && just check")
    steps.append(f"just run {agent}   (one scripted session on this machine)")
    if bootstrapped.get("git", "").startswith("initialised"):
        steps += [
            "git add -A && git commit -m 'new agent'",
            "create the GitHub repository and push main",
        ]
    else:
        steps.append("commit in your repository and push main (git was not initialised here)")
    steps += [
        "repository settings: secrets REGISTRY_USERNAME and REGISTRY_PASSWORD (from the Kubani operator),",
        "  variable RELEASE_RUNNER (the self-hosted runner that reaches the registry)",
        f"Kubani operator: just agent register {tenant} <owner>/<repo> {agent} --source <this checkout>",
        f"release: git tag {agent}/v0.1.0 && git push --tags   (from merged main, one tag at a time)",
        f"Kubani operator: just agent stage {agent} {agent}/v0.1.0, then just agent approve <release>",
    ]
    return "next:\n" + "\n".join(f"  {step}" for step in steps)


def rename(repo: Path, old: str, new: str) -> list[Path]:
    """Rename an agent: directory, package, manifest, Agent, project name, tests, instructions.

    For repositories created from the GitHub template, whose agent carries a placeholder
    name. Only the known fields are edited (never prose), every new file content is
    computed first, files are written, and the directories move last; a failure before
    the moves leaves the original agent untouched.
    """
    for name in (old, new):
        if not layout.NAME.match(name):
            raise RenderError(f"{name!r} is not a DNS label of at most 32 characters")
    if old == new:
        raise RenderError("old and new names are the same")
    new_package = new.replace("-", "_")
    if (
        not new_package.isidentifier()
        or keyword.iskeyword(new_package)
        or new_package in RESERVED_PACKAGES
    ):
        raise RenderError(f"{new!r} does not give a usable Python package name ({new_package!r})")
    source = repo / "agents" / old
    target = repo / "agents" / new
    if not source.is_dir():
        raise RenderError(f"no agent directory agents/{old}")
    if target.exists():
        raise RenderError(f"agents/{new} already exists")
    package = layout.AgentDir(source).package_dir()
    if package is None:
        raise RenderError(f"agents/{old}/src must hold exactly one package")
    old_package = package.name

    edits: dict[Path, str] = {}

    def edit(path: Path, replacements: list[tuple[re.Pattern[str], str, bool]]) -> None:
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8")
        for pattern, replacement, required in replacements:
            text, count = pattern.subn(replacement, text)
            if required and count == 0:
                raise RenderError(f"{path.relative_to(repo)}: expected {pattern.pattern!r}")
        edits[path] = text

    module = re.escape(old_package)
    edit(
        source / "agent.yaml",
        [
            (re.compile(rf"^name: {re.escape(old)}$", re.MULTILINE), f"name: {new}", True),
            (
                re.compile(rf"^entrypoint: {module}\.", re.MULTILINE),
                f"entrypoint: {new_package}.",
                True,
            ),
            (
                re.compile(rf"^instructions: {module}/", re.MULTILINE),
                f"instructions: {new_package}/",
                True,
            ),
        ],
    )
    edit(
        source / "deploy" / "agent.yaml",
        [(re.compile(rf"^  name: {re.escape(old)}$", re.MULTILINE), f"  name: {new}", True)],
    )
    edit(
        source / "pyproject.toml",
        [
            (
                re.compile(rf'^name = "zetra-agent-{re.escape(old)}"$', re.MULTILINE),
                f'name = "zetra-agent-{new}"',
                True,
            ),
            (re.compile(rf"/app/{module}\b"), f"/app/{new_package}", False),
        ],
    )
    # The package as the first element of a dotted path (never a submodule of the same name),
    # and the tests' PACKAGE constant.
    module_ref = re.compile(rf"(?<![A-Za-z0-9_.\"']){module}(?=\.[A-Za-z_])")
    module_str = re.compile(rf'^PACKAGE = "{module}"$', re.MULTILINE)
    for path in sorted(source.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        edit(
            path,
            [(module_ref, new_package, False), (module_str, f'PACKAGE = "{new_package}"', False)],
        )
    edit(
        package / "instructions" / "base.md",
        [(re.compile(rf"^You are {re.escape(old)}\.", re.MULTILINE), f"You are {new}.", False)],
    )

    changed = []
    try:
        for path, text in edits.items():
            if path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
                changed.append(path)
        if old_package != new_package:
            package.rename(source / "src" / new_package)
        source.rename(target)
    except OSError as exc:
        raise RenderError(
            f"rename stopped: {exc}; the agent may be partly renamed under agents/{old}"
        ) from exc
    return [target, *(target / p.relative_to(source) for p in changed)]
