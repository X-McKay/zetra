"""What an agent repository and each agent directory must contain, and the static import
check of spec 14.1.

The layout is the same for a repository with one agent and for one with thirteen: agents
live in `agents/<name>/`, the repository root is a uv workspace whose single `uv.lock` is
the image lock (`AgentRelease.spec.lockSha256`), and the root files that are the same for
every repository (Dockerfile, CI, release shim, justfile, root pyproject) are rendered
from `zetra_agents.render`, never hand-edited.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from zetra_agents import schema

FORBIDDEN_IMPORTS = {"httpx", "requests", "urllib", "socket", "aiohttp", "subprocess"}
NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
REQUIRED = ("agent.yaml", "deploy/agent.yaml", "pyproject.toml")
REPOSITORY_REQUIRED = ("pyproject.toml", "uv.lock")
# The conformance agent's probes attempt what every other agent must not (spec 15.1);
# conformance-b is the same code under a second identity (T26).
NETWORK_ALLOWED = {
    "conformance",
    "conformance-b",
    "conformance-budget-day",
    "conformance-budget-day-retry",
    "conformance-budget-day-retry2",
    "conformance-budget-session",
    "conformance-transport",  # T27 credential-free wire and foreign-CA negatives only
}
# Conformance fixtures whose image replaces the installed SDK boundary driver: built with
# the Dockerfile's `fixture` target, which copies the package's step.py over the SDK's and
# runs the agent's verify_image.py. Being listed here is the only way onto that target;
# `check` refuses the fixture files on any other agent and requires them on these. The
# set is frozen while broad qualification is paused.
FIXTURES = {
    "conformance-budget-day",
    "conformance-budget-day-retry",
    "conformance-budget-day-retry2",
    "conformance-budget-session",
    "conformance-checkpoint",
    "conformance-lifecycle",
    "conformance-performance",
    "conformance-protocol",
    "conformance-transport",
}
VERIFY_IMAGE = "verify_image.py"
# The effects a tool may declare: `EFFECTS` of agent_runtime.tools, which a test holds this
# to. `remote_write` is not among them and has its own refusal.
EFFECTS = ("workspace_read", "workspace_write", "remote_read")
# A fixture driver keeps the SDK's step module under this name and delegates to it.
SDK_STEP_BACKUP = re.compile(r"agent_runtime\._([a-z]+)_sdk_step")
IMAGE_TARGETS = ("runtime", "fixture")


@dataclass(frozen=True)
class AgentDir:
    root: Path

    @property
    def name(self) -> str:
        return self.root.name

    @property
    def repo(self) -> Path:
        return self.root.parent.parent

    @property
    def manifest_path(self) -> Path:
        return self.root / "agent.yaml"

    @property
    def lock_path(self) -> Path:
        """The repository lock: one workspace lock serves every agent in the repository."""
        return self.repo / "uv.lock"

    @property
    def agent_cr_path(self) -> Path:
        return self.root / "deploy" / "agent.yaml"

    @property
    def src(self) -> Path:
        return self.root / "src"

    def package_dir(self) -> Path | None:
        """The one package under src/, copied to /app/<package> in the image."""
        if not self.src.is_dir():
            return None
        packages = [p for p in sorted(self.src.iterdir()) if p.is_dir() and p.name != "__pycache__"]
        return packages[0] if len(packages) == 1 else None

    def manifest(self) -> dict[str, Any]:
        return yaml.safe_load(self.manifest_path.read_text(encoding="utf-8"))

    def agent_cr(self) -> dict[str, Any]:
        return yaml.safe_load(self.agent_cr_path.read_text(encoding="utf-8"))

    def fixture_files(self) -> list[Path]:
        """The files that make an image a fixture image, whichever of them are present."""
        found = [p for p in (self.root / VERIFY_IMAGE,) if p.is_file()]
        package = self.package_dir()
        if package is not None and (package / "step.py").is_file():
            found.append(package / "step.py")
        return found


def find(repo: Path, name: str) -> AgentDir:
    agent = AgentDir(repo / "agents" / name)
    if not agent.root.is_dir():
        raise SystemExit(f"no agent directory agents/{name}")
    return agent


def all_agents(repo: Path) -> list[AgentDir]:
    agents = repo / "agents"
    if not agents.is_dir():
        return []
    return [AgentDir(p) for p in sorted(agents.iterdir()) if p.is_dir()]


def image_target(agent: AgentDir | str) -> str:
    name = agent if isinstance(agent, str) else agent.name
    return "fixture" if name in FIXTURES else "runtime"


def forbidden_imports(src: Path) -> list[str]:
    """Imports of network or process modules anywhere under the agent's source."""
    found = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if name.split(".")[0] in FORBIDDEN_IMPORTS:
                    found.append(f"{path}:{node.lineno}: imports {name}")
    return found


def check_files(agent: AgentDir) -> list[str]:
    return [f"{agent.name}: missing {f}" for f in REQUIRED if not (agent.root / f).is_file()]


def _read(path: Path, label: str, name: str) -> tuple[dict[str, Any] | None, str | None]:
    """The YAML mapping in `path`, or the one-line reason it is not one."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        reason = str(getattr(exc, "problem", None) or "cannot be parsed").splitlines()[0]
        return None, f"{name}: {label} is malformed YAML{where}: {reason}"
    except UnicodeDecodeError:
        return None, f"{name}: {label} is not UTF-8 text"
    if document is None:
        return None, f"{name}: {label} is empty; it must be a mapping"
    if not isinstance(document, dict):
        return None, f"{name}: {label} must be a mapping, not {_kind(document)}"
    return document, None


def _kind(value: Any) -> str:
    """A value's YAML type, with an article."""
    if isinstance(value, dict):
        return "a mapping"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    return "null"


def _manifest_shape(name: str, manifest: dict[str, Any]) -> list[str]:
    """Problems with the shape of agent.yaml's `model` and `tools`, which the later
    checks read as a mapping and a list of mappings."""
    problems = []
    if "model" in manifest and manifest["model"] is not None:
        model = manifest["model"]
        if not isinstance(model, dict) or not all(
            isinstance(model.get(k), str) and model.get(k) for k in ("backend", "name")
        ):
            problems.append(
                f"{name}: agent.yaml model is malformed: it must be a mapping with a backend "
                "and a name (strings), or null for an agent that calls no model"
            )
    tools = manifest.get("tools")
    if tools is not None and not isinstance(tools, list):
        problems.append(
            f"{name}: agent.yaml tools must be a list of mappings with name and effect, "
            f"not {_kind(tools)}"
        )
    return problems


def _tool_problems(name: str, tools: list[Any]) -> list[str]:
    problems = []
    seen: set[str] = set()
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            problems.append(
                f"{name}: agent.yaml tools[{index}] is {_kind(tool)}; each tool must be a "
                "mapping with name and effect"
            )
            continue
        tool_name, effect = tool.get("name"), tool.get("effect")
        if not isinstance(tool_name, str) or not tool_name:
            problems.append(f"{name}: agent.yaml tools[{index}] has no name (a string)")
            continue
        if tool_name in seen:
            problems.append(
                f"{name}: agent.yaml lists the tool {tool_name!r} more than once; "
                "tool names must be unique"
            )
        seen.add(tool_name)
        if effect == "remote_write":
            problems.append(f"{name}: remote_write tools are not admitted")
        elif effect is None:
            problems.append(
                f"{name}: agent.yaml tool {tool_name!r} has no effect; set effect to "
                f"one of {', '.join(EFFECTS)}"
            )
        elif not isinstance(effect, str) or effect not in EFFECTS:
            problems.append(
                f"{name}: agent.yaml tool {tool_name!r} has effect {effect!r}, which is not "
                f"one of {', '.join(EFFECTS)}; set effect to the one the tool has"
            )
    return problems


def _cr_shape(name: str, cr: dict[str, Any]) -> list[str]:
    """Problems that stop the later checks from reading the Agent resource at all."""
    problems = []
    metadata = cr.get("metadata")
    if not isinstance(metadata, dict):
        problems.append(
            f"{name}: deploy/agent.yaml has no metadata mapping; it needs metadata.name "
            "and metadata.namespace"
        )
    return problems


def _grant_problems(name: str, spec: dict[str, Any]) -> list[str]:
    """Rules about the tool-backend grants that hold once the spec is inside the CRD."""
    problems = []
    caps = spec.get("capabilities") or {}
    mode = (spec.get("identity") or {}).get("mode")
    for index, entry in enumerate(caps.get("mcp") or []):
        if mode == "agent" and entry.get("delegation") == "on-behalf-of":
            problems.append(
                f"{name}: deploy/agent.yaml spec.capabilities.mcp[{index}] (backend "
                f"{entry.get('backend')}) has delegation on-behalf-of, but spec.identity.mode "
                "is agent, so there is no person to act for; set spec.identity.mode to "
                "on-behalf-of or the grant's delegation to agent"
            )
    return problems


def check(agent: AgentDir) -> list[str]:
    """Every problem with one agent directory; an empty list means it is releasable."""
    problems = check_files(agent)
    if problems:
        return problems
    if not NAME.match(agent.name):
        problems.append(f"{agent.name}: directory name is not a DNS label of at most 32")
    manifest, refused = _read(agent.manifest_path, "agent.yaml", agent.name)
    cr, cr_refused = _read(agent.agent_cr_path, "deploy/agent.yaml", agent.name)
    problems += [p for p in (refused, cr_refused) if p]
    if manifest is None or cr is None:
        return problems
    shape = _manifest_shape(agent.name, manifest) + _cr_shape(agent.name, cr)
    if shape:
        return problems + shape
    if manifest.get("name") != agent.name:
        problems.append(f"{agent.name}: agent.yaml name is {manifest.get('name')!r}")
    if cr.get("kind") != "Agent" or cr["metadata"].get("name") != agent.name:
        problems.append(f"{agent.name}: deploy/agent.yaml must be the Agent named {agent.name}")
    refused_spec = schema.problems(cr.get("spec"))
    if refused_spec:
        # The checks below read the spec as the CRD shapes it, so they wait for one that is.
        return problems + [
            f"{agent.name}: deploy/agent.yaml is outside the Agent CRD: {p}" for p in refused_spec
        ]
    caps = cr["spec"].get("capabilities") or {}
    model = manifest.get("model")
    if "model" not in manifest:
        problems.append(f"{agent.name}: manifest must declare model or explicit model: null")
    elif model is None:
        if caps.get("models") != []:
            problems.append(
                f"{agent.name}: model: null requires an explicit empty model grant list"
            )
    else:
        models = {m["backend"]: set(m["models"]) for m in caps.get("models") or []}
        if model["name"] not in models.get(model["backend"], set()):
            problems.append(f"{agent.name}: manifest model is not granted by the Agent")
    problems += _tool_problems(agent.name, manifest.get("tools") or [])
    problems += _grant_problems(agent.name, cr["spec"])
    if agent.src.is_dir() and agent.package_dir() is None:
        problems.append(f"{agent.name}: src/ must hold exactly one package directory")
    elif agent.src.is_dir():
        # The whole of src/ is copied to /app, which precedes site-packages on sys.path.
        strays = [
            p.name
            for p in sorted(agent.src.iterdir())
            if p != agent.package_dir() and p.name != "__pycache__"
        ]
        if strays:
            problems.append(f"{agent.name}: src/ holds more than the package: {', '.join(strays)}")
    if agent.src.is_dir() and agent.name not in NETWORK_ALLOWED:
        problems += [f"{agent.name}: {p}" for p in forbidden_imports(agent.src)]
    problems += _fixture_problems(agent)
    return problems


def _fixture_problems(agent: AgentDir) -> list[str]:
    files = agent.fixture_files()
    if agent.name not in FIXTURES:
        return [
            f"{agent.name}: {p.relative_to(agent.root).as_posix()} belongs to a conformance "
            "fixture; only agents listed in FIXTURES build with the fixture image target"
            for p in files
        ]
    present = {p.name if p.name == VERIFY_IMAGE else "step.py" for p in files}
    problems = [
        f"{agent.name}: fixture is missing {name}"
        for name in (VERIFY_IMAGE, "step.py")
        if name not in present
    ]
    step = next((p for p in files if p.name == "step.py"), None)
    if step is not None and not SDK_STEP_BACKUP.search(step.read_text(encoding="utf-8")):
        problems.append(
            f"{agent.name}: the fixture driver must delegate to agent_runtime._<name>_sdk_step"
        )
    return problems


def check_repository(repo: Path) -> list[str]:
    """Problems with the repository root that are not about one agent."""
    problems = [f"repository: missing {f}" for f in REPOSITORY_REQUIRED if not (repo / f).is_file()]
    if not (repo / "agents").is_dir():
        problems.append("repository: missing agents/")
    return problems
