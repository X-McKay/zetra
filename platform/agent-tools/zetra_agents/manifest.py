"""The manifest's `tools:` list is derived from the code, not written twice.

`create_agent` is where a tool and its effect are declared. `agent.yaml` must list the
same names and effects, because the manifest is what a release hashes and what the SDK
holds the image to. `zetra-agents render` writes that list from the code and
`render --check` fails when they differ, so the list is never typed by hand.

Reading the code means importing it: each agent's factory is called once in a separate
process, in a throwaway copy of the image layout, with an MCP that refuses every call.
That needs the agent's dependencies installed (`just setup`).

Conformance fixtures (`layout.FIXTURES`) are not derived: their factories refuse to run
without the step driver their own image installs, and that image build already holds
their tools to the manifest.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from zetra_agents import testing
from zetra_agents.layout import AgentDir

TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
EFFECT = re.compile(r"^[a-z_]{1,32}$")
TOOLS_KEY = re.compile(r"tools:(\s|$)")
# The SDK's build_agent without the manifest comparison, which is the thing being derived.
READER = """
import importlib, json, os, pathlib, sys, types
app = pathlib.Path(os.environ["AGENT_APP_DIR"])
entrypoint, instructions, out = sys.argv[1:4]
async def mcp(*args, **kwargs):
    raise RuntimeError("no MCP while the tool list is read")
sys.path.insert(1, str(app))
module, _, factory = entrypoint.partition(":")
ctx = types.SimpleNamespace(
    workspace=pathlib.Path(os.environ["AGENT_SANDBOX_DIR"]) / "workspace",
    instructions=(app / instructions).read_text(encoding="utf-8"),
    mcp=mcp,
)
listed = getattr(importlib.import_module(module), factory)(ctx)["tools"]
tools = [{"name": t.fn.__name__, "effect": t.effect} for t in listed]
pathlib.Path(out).write_text(json.dumps(tools), encoding="utf-8")
"""


class ToolsError(ValueError):
    """The tool list could not be read from the code."""


def declared(agent: AgentDir) -> dict:
    """The manifest as a mapping; what else is wrong with it is `check`'s to say."""
    try:
        document = agent.manifest()
    except yaml.YAMLError:
        raise ToolsError("agent.yaml is not YAML") from None
    if not isinstance(document, dict):
        raise ToolsError("agent.yaml is not a mapping")
    return document


def code_tools(agent: AgentDir) -> list[dict[str, str]]:
    """The tools `create_agent` returns, in its order, as the manifest lists them."""
    manifest = declared(agent)
    with tempfile.TemporaryDirectory(prefix="zetra-agents-tools-") as tmp:
        root = Path(tmp)
        testing.lay_out(agent, root / "app")
        (root / "sandbox" / "workspace").mkdir(parents=True)
        out = root / "tools.json"
        environment = testing.child_environment(root / "app", root / "sandbox")
        try:
            done = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    READER,
                    str(manifest.get("entrypoint")),
                    str(manifest.get("instructions")),
                    str(out),
                ],
                env=environment,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ToolsError("create_agent did not return within 60 seconds") from None
        if done.returncode != 0 or not out.is_file():
            last = (done.stderr.strip().splitlines() or ["no output"])[-1]
            hint = (
                "; install the workspace first: `just setup`"
                if "ModuleNotFoundError" in last
                else ""
            )
            raise ToolsError(f"cannot read the tools from the code: {last[:300]}{hint}")
        tools = json.loads(out.read_text(encoding="utf-8"))
    names = [t["name"] for t in tools]
    if len(set(names)) != len(names):
        raise ToolsError("create_agent returns two tools with the same name")
    for tool in tools:
        if not TOOL_NAME.match(tool["name"]) or not EFFECT.match(tool["effect"]):
            raise ToolsError(f"tool {tool['name']!r} has a name or effect that cannot be listed")
    return tools


def block(tools: list[dict[str, str]]) -> str:
    if not tools:
        return "tools: []\n"
    return "tools:\n" + "".join(
        f"  - name: {_scalar(t['name'])}\n    effect: {t['effect']}\n" for t in tools
    )


def _scalar(name: str) -> str:
    """The name as YAML reads back as that text: `on`, `yes` and `null` need quotes."""
    try:
        plain = yaml.safe_load(name) == name
    except yaml.YAMLError:
        plain = False
    return name if plain else json.dumps(name)


def _mapping(tools: object) -> dict | None:
    """name -> effect, or None when the list is not a list of distinct name/effect pairs."""
    if not isinstance(tools, list) or not all(isinstance(t, dict) for t in tools):
        return None
    mapping = {t.get("name"): t.get("effect") for t in tools}
    return mapping if len(mapping) == len(tools) else None


def with_tools(text: str, tools: list[dict[str, str]]) -> str:
    """`text` with its top-level `tools:` block replaced, comments inside the block
    included, and everything outside it kept (a missing final newline is added)."""
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += newline
    starts = [i for i, line in enumerate(lines) if TOOLS_KEY.match(line)]
    if len(starts) > 1:
        raise ToolsError("agent.yaml has more than one top-level tools: key")
    try:
        before = yaml.safe_load(text)
    except yaml.YAMLError:
        before = None
    if not starts and isinstance(before, dict) and "tools" in before:
        # A quoted or oddly spaced key: appending a block would leave two.
        raise ToolsError("agent.yaml: write `tools:` as a plain key at the left margin")
    if not starts:
        updated = "".join(lines) + block(tools).replace("\n", newline)
    else:
        start = end = starts[0]
        end += 1
        # The value: indented lines, a sequence written at the key's own indentation, and
        # blank lines between them (blank lines after the last entry stay where they are).
        while end < len(lines) and (lines[end][0] in " \t-" or not lines[end].strip()):
            end += 1
        while not lines[end - 1].strip():
            end -= 1
        updated = (
            "".join(lines[:start]) + block(tools).replace("\n", newline) + "".join(lines[end:])
        )
    try:
        rewritten = yaml.safe_load(updated)
    except yaml.YAMLError:
        rewritten = None
    if not isinstance(rewritten, dict) or _mapping(rewritten.get("tools")) != _mapping(tools):
        raise ToolsError("agent.yaml tools: could not be rewritten in place; edit it by hand")
    return updated


def describe(declared: dict | None, wanted: dict) -> str | None:
    """How a manifest's name -> effect mapping differs from the code's, or None."""
    if declared == wanted:
        return None
    if declared is None:
        return "tools: is not a list of distinct name and effect pairs"
    changes = [
        f"{name} ({wanted[name]}) is in the code but not listed"
        for name in wanted.keys() - declared.keys()
    ]
    changes += [f"{name} is listed but not in the code" for name in declared.keys() - wanted.keys()]
    changes += [
        f"{name} is {wanted[name]} in the code but listed {declared[name]}"
        for name in wanted.keys() & declared.keys()
        if wanted[name] != declared[name]
    ]
    return "; ".join(sorted(changes))


def difference(agent: AgentDir) -> str | None:
    """What `render` would change in this agent's manifest, or None when it agrees."""
    return describe(_mapping(declared(agent).get("tools")), _mapping(code_tools(agent)))


def write(agent: AgentDir) -> bool:
    """Rewrite the manifest's tool list from the code; True when the file changed. A list
    that already agrees keeps its order and layout: the manifest's bytes are hashed into
    every release."""
    tools = code_tools(agent)
    if _mapping(declared(agent).get("tools")) == _mapping(tools):
        return False
    # Bytes, not text mode: universal newlines would rewrite a CRLF file's every line.
    text = agent.manifest_path.read_bytes().decode("utf-8")
    agent.manifest_path.write_bytes(with_tools(text, tools).encode("utf-8"))
    return True
