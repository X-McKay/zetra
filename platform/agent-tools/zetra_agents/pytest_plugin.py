"""pytest fixtures for agent tests, loaded wherever `zetra-agents` is installed.

    async def test_pods_are_listed(agent):
        agent.mcp.reply("pods_list_in_namespace", "demo-1 Running")
        result = await agent.call("list_pods")
        assert agent.mcp.calls == [("kubernetes", "pods_list_in_namespace", {...})]

`agent` is the agent whose directory holds the test, built by the SDK exactly as its
image builds it; a manifest and code that disagree fail there, with the SDK's own error.

The plugin also makes one pytest process serve a whole repository. While a test under
`agents/<name>/` is collected or run, that agent's `src/` is importable and no other
agent's code is: every agent is alone in its image, so two agents may name a package or a
test file alike, and `pytest` at the repository root must give each test its own agent's
modules as `pytest agents/<name>` does. The rest of the pytest configuration is the
rendered root `pyproject.toml`.
"""

import os
import sys
from pathlib import Path
from types import ModuleType

import pytest


def find_agent_dir(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        if (directory / "agent.yaml").is_file() and (directory / "deploy/agent.yaml").is_file():
            return directory
    return None


class AgentImports:
    """Whose code `import` finds: one agent directory at a time.

    Entering an agent puts its `src/` first on `sys.path`. Leaving it takes every
    `sys.path` entry and every imported module that lies under the agent directory out
    of the interpreter and keeps them, and entering it again puts them back, so a module
    object is never imported twice and an import made while a test runs resolves as it
    did when the test was collected.
    """

    def __init__(self) -> None:
        self.active: Path | None = None
        self.kept: dict[Path, tuple[list[str], dict[str, ModuleType]]] = {}
        self.owners: dict[Path, Path | None] = {}

    def enter_for(self, path: Path) -> None:
        if path not in self.owners:
            self.owners[path] = find_agent_dir(path)
        self.enter(self.owners[path])

    def enter(self, agent: Path | None) -> None:
        if agent == self.active:
            return
        if self.active is not None:
            self.kept[self.active] = _take(self.active)
        self.active = agent
        if agent is None:
            return
        paths, modules = self.kept.pop(agent, ([], {}))
        src = str(agent / "src")
        if src not in paths and src not in sys.path and os.path.isdir(src):
            paths.append(src)
        sys.path[:0] = paths
        sys.modules.update(modules)


def _take(agent: Path) -> tuple[list[str], dict[str, ModuleType]]:
    """Remove and return the `sys.path` entries and imported modules under `agent`."""
    roots = tuple({str(agent) + os.sep, os.path.realpath(agent) + os.sep})

    def inside(origin: object) -> bool:
        return isinstance(origin, str) and (origin + os.sep).startswith(roots)

    # Choose the modules before removing anything. A package without `__init__.py` works
    # its path out again whenever the path is read, from `sys.path` or from its parent
    # package in `sys.modules`: read after the agent has left `sys.path` it can name
    # another directory, and read after the parent has been removed it raises.
    names = []
    for name, module in list(sys.modules.items()):
        # Read from the module's namespace: a module may compute its other attributes.
        namespace = getattr(module, "__dict__", {})
        locations = namespace.get("__path__")
        origins = [namespace.get("__file__"), *(() if locations is None else locations)]
        if any(inside(origin) for origin in origins):
            names.append(name)
    paths = [entry for entry in sys.path if inside(entry)]
    sys.path[:] = [entry for entry in sys.path if not inside(entry)]
    return paths, {name: sys.modules.pop(name) for name in names}


_IMPORTS = pytest.StashKey[AgentImports]()


def _imports(config) -> AgentImports:
    return config.stash.setdefault(_IMPORTS, AgentImports())


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config) -> None:
    """pytest imports the conftest files of the paths on the command line before it
    collects anything. When those paths (or, with none, the directory pytest runs in)
    lie in one agent, as in `pytest agents/<name>`, that agent is entered first."""
    start = early_config.invocation_params.dir
    named = [name.split("::")[0] for name in early_config.known_args_namespace.file_or_dir]
    owners = {find_agent_dir(Path(os.path.abspath(start / name))) for name in named or ["."]}
    if len(owners) == 1:
        _imports(early_config).enter(owners.pop())


@pytest.hookimpl(tryfirst=True)
def pytest_collectstart(collector) -> None:
    _imports(collector.config).enter_for(collector.path)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item) -> None:
    _imports(item.config).enter_for(item.path)


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(request):
    """A fixture is finished among the modules it was set up among. pytest finishes a
    session fixture during the last test of the run, which may be another agent's, and
    what the fixture imports then is still its own agent's code."""
    imports = _imports(request.config)
    owner, interrupted = imports.active, []

    def enter_owner() -> None:
        interrupted.append(imports.active)
        imports.enter(owner)

    def leave_owner() -> None:
        if interrupted:
            imports.enter(interrupted.pop())

    # A fixture's finalizers run last added first, and its own teardown is added between.
    request.addfinalizer(leave_owner)
    result = yield
    request.addfinalizer(enter_owner)
    return result


@pytest.fixture
def agent_dir(request) -> Path:
    """The `agents/<name>/` directory the running test belongs to."""
    found = find_agent_dir(Path(request.node.path).resolve().parent)
    if found is None:
        raise pytest.UsageError(
            f"{request.node.path} is not under an agent directory "
            "(one holding agent.yaml and deploy/agent.yaml)"
        )
    return found


@pytest.fixture
def agent(agent_dir, tmp_path_factory):
    """The agent under test: `.tools`, `.call(name, **arguments)`, `.schema(name)`,
    `.instructions`, `.manifest`, `.workspace` and the scripted `.mcp`. The test fails
    if the agent made an MCP call its Agent does not grant or the test did not script."""
    from zetra_agents import testing

    with testing.loaded(agent_dir, tmp_path_factory.mktemp("agent")) as local:
        yield local
        problems = local.mcp.problems() if local.mcp.strict else []
    if problems:
        pytest.fail("\n".join(problems), pytrace=False)
