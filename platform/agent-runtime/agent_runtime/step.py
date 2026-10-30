"""One boundary: load state, call the model or one tool, persist state, print the result
(spec 10.4, 11.8).

Exit codes: 0 success; 2 completion (done: true); 3 agent error (terminal); 4 budget
(advisory counter, or a 429 from the gateway); 5 protocol error. Anything else, such as an
uncaught exception (1), is an unknown outcome for the runner.
"""

import asyncio
import importlib
import inspect
import json
import math
import os
import pathlib
import re
import sys
import time
import types

import yaml

from agent_runtime import confine
from agent_runtime.identity import sandbox_dir, state_dir, write_private
from agent_runtime.tools import EFFECTS, Tool

MAX_INPUT = 65536
MAX_OUTPUT = 65536
MAX_TOOL_RESULT = 65536
MAX_CONTEXT_BYTES = 200_000  # context management: oldest tool results are dropped first
MAX_SUMMARY = 512
DROPPED = "[dropped for context size]"
HEADER = re.compile(rb"^[0-9]{1,6}\r?\n$")
SECRET = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|Bearer [A-Za-z0-9._~+/=-]{20,}")


class ProtocolError(Exception):
    pass


def app_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("AGENT_APP_DIR", "/app"))


def advisory_budget() -> int:
    return int(os.environ.get("AGENT_TOKENS_PER_SESSION", "200000"))  # the APS enforces


def read_request(stream) -> dict:
    """Length-prefixed input: a decimal byte count line, then exactly that many bytes.
    Never waits for EOF: it reads the header line, then exactly n bytes."""
    header = stream.readline(16)
    if not HEADER.match(header):
        raise ProtocolError("bad length header")
    n = int(header)
    if not 0 < n <= MAX_INPUT:
        raise ProtocolError("length out of range")
    body = stream.read(n)
    if len(body) != n:
        raise ProtocolError("short body")
    try:
        req = json.loads(body.decode("utf-8"))
    except ValueError:
        raise ProtocolError("invalid JSON") from None
    if not isinstance(req, dict) or "task" not in req:
        raise ProtocolError("request is not an object with a task")
    b = req.get("boundary")
    if type(b) is not int or not 0 <= b < 1_000_000:
        raise ProtocolError("bad boundary")
    return req


def clip(value, limit: int, items: int | None = None, depth: int = 0):
    """Truncate a structure leaf by leaf before serialization, so the result is always valid
    JSON: strings to `limit` bytes, containers to `items` entries, nesting to 32 levels."""
    if isinstance(value, str):
        b = value.encode()
        return value if len(b) <= limit else b[:limit].decode("utf-8", "ignore") + "...[truncated]"
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if depth >= 32:
        return "[nested]"
    if isinstance(value, dict):
        entries = list(value.items())
        kept = entries if items is None else entries[:items]
        out = {clip(str(k), 256): clip(v, limit, items, depth + 1) for k, v in kept}
        if len(kept) < len(entries):
            out["...[truncated]"] = f"{len(entries) - len(kept)} more keys"
        return out
    if isinstance(value, list | tuple):
        kept = value if items is None else value[:items]
        out = [clip(v, limit, items, depth + 1) for v in kept]
        if len(kept) < len(value):
            out.append(f"...[{len(value) - len(kept)} more items]")
        return out
    return clip(str(value), limit)


def bounded(value, max_bytes: int):
    """The least-truncated form of `value` whose JSON fits in `max_bytes`."""
    for limit, items in ((16384, None), (4096, 256), (1024, 64), (256, 16), (64, 4)):
        v = clip(value, limit, items)
        if len(json.dumps(v)) <= max_bytes:
            return v
    return {"truncated": True}


def short(text, n: int = MAX_SUMMARY) -> str:
    text = SECRET.sub("[redacted]", str(text))
    return text if len(text) <= n else text[: n - 3] + "..."


def load_manifest() -> dict:
    with open(app_dir() / "agent.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_agent(manifest: dict):
    """Import the agent and check its tools match the manifest exactly. Errors are agent errors."""
    app = app_dir().resolve()
    module, _, factory = manifest["entrypoint"].partition(":")
    path = (app / manifest["instructions"]).resolve()
    if not path.is_relative_to(app):
        raise ValueError("instructions outside the app directory")
    instructions = path.read_text(encoding="utf-8")
    declared = {t["name"]: t["effect"] for t in manifest["tools"]}
    if len(declared) != len(manifest["tools"]) or not set(declared.values()) <= EFFECTS:
        raise ValueError("manifest tools are duplicated or not admitted")
    from agent_runtime.mcp import call_tool

    if str(app) not in sys.path:
        sys.path.insert(1, str(app))
    ctx = types.SimpleNamespace(
        workspace=sandbox_dir() / "workspace", instructions=instructions, mcp=call_tool
    )
    agent = getattr(importlib.import_module(module), factory)(ctx)
    listed = agent["tools"]
    if not all(isinstance(t, Tool) for t in listed):
        raise ValueError("tools must be Tool instances")
    tools = {t.fn.__name__: t for t in listed}
    if len(tools) != len(listed) or {n: t.effect for n, t in tools.items()} != declared:
        raise ValueError("the image's tools do not match its manifest")
    return instructions, tools


def schema(tool: Tool) -> dict:
    sig = inspect.signature(tool.fn)
    props = {n: {"type": "string"} for n in sig.parameters}  # string-typed in this profile
    required = [n for n, p in sig.parameters.items() if p.default is p.empty]
    return {
        "type": "function",
        "function": {
            "name": tool.fn.__name__,
            "description": tool.fn.__doc__.strip(),
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def trim_context(messages: list) -> list:
    """Drop the oldest tool results (never the system or first user message) until it fits."""
    while len(json.dumps(messages).encode()) > MAX_CONTEXT_BYTES:
        idx = next(
            (
                i
                for i, m in enumerate(messages)
                if i > 1 and m["role"] == "tool" and m["content"] != DROPPED
            ),
            None,
        )
        if idx is None:
            break
        messages[idx] = {**messages[idx], "content": DROPPED}
    return messages


def emit(out: dict, rc: int):
    """Print one JSON document of at most 64 KiB and exit. done is true exactly when rc is 2."""
    out["done"] = rc == 2
    out["summary"] = short(out.get("summary") or "")
    if out.get("tool") is not None:
        out["tool"] = short(out["tool"], 128)
    data = json.dumps(out)  # ASCII-only, so characters are bytes
    if len(data) > MAX_OUTPUT:  # unreachable with the bounds above; never print a cut document
        data = json.dumps({**out, "summary": "output too large", "tool": None})
    sys.stdout.write(data)
    sys.stdout.flush()
    sys.exit(rc)


def run_tool(call: dict, tools: dict) -> tuple[str, object, int]:
    from agent_runtime.gateway import BudgetExhausted, PolicyDenied

    fn = call.get("function") or {}
    name = str(fn.get("name"))
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except (TypeError, ValueError):
        args = None
    if name not in tools or not isinstance(args, dict):
        return name, {"error": "unknown tool or bad arguments"}, 3
    try:
        return name, asyncio.run(tools[name].fn(**args)), 0
    except BudgetExhausted:
        return name, {"error": "budget_exhausted"}, 4
    except PolicyDenied:  # a policy decision: terminal for this call, reported to the model
        return name, {"error": "policy_denied"}, 0
    except Exception as e:  # noqa: BLE001 - tool errors are data for the model, not a crash
        return name, {"error": type(e).__name__}, 0


def run_model(state: dict, manifest: dict, tools: dict, out: dict) -> int:
    import openai

    from agent_runtime.models import chat_client

    state["messages"] = trim_context(state["messages"])
    model = manifest["model"]
    try:
        resp = chat_client().chat.completions.create(
            model=model["name"],
            messages=state["messages"],
            tools=[schema(t) for t in tools.values()],
            tool_choice="auto",
            max_tokens=model.get("max_tokens", 1024),
            temperature=model.get("temperature", 0),
            parallel_tool_calls=False,
        )
    except openai.RateLimitError:
        out["summary"] = "budget exhausted (429)"
        return 4
    except (openai.PermissionDeniedError, openai.AuthenticationError) as e:
        out["summary"] = f"model call refused ({e.status_code})"
        return 3
    choice = resp.choices[0]
    msg = {"role": "assistant", "content": choice.message.content or ""}
    calls = [tc for tc in choice.message.tool_calls or [] if getattr(tc, "function", None)]
    if calls:  # only the first tool call is ever executed
        tc = calls[0]
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
        ]
    state["messages"].append(msg)
    usage = resp.usage
    prompt = (usage.prompt_tokens or 0) if usage else 0
    completion = (usage.completion_tokens or 0) if usage else 0
    state["usage"] += prompt + completion
    out.update(
        kind="model",
        usage={"input_tokens": prompt, "output_tokens": completion},
        summary=choice.message.content or "tool call",
    )
    if not calls:
        return 2
    if state["usage"] > advisory_budget():
        return 4  # advisory: the trusted limit is the gateway's
    return 0


def main() -> None:
    confine.apply()
    try:
        req = read_request(sys.stdin.buffer)
    except ProtocolError as e:
        print(f"agent_runtime.step: {e}", file=sys.stderr)
        sys.exit(5)
    n = req["boundary"]
    out = {
        "boundary": n,
        "kind": "model",
        "done": False,
        "tool": None,
        "usage": None,
        "summary": "",
    }
    sdir = state_dir()
    sdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (sandbox_dir() / "workspace").mkdir(parents=True, exist_ok=True)
    state_file = sdir / "state.json"
    if state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))
    else:
        state = {"messages": [], "usage": 0, "next": 0}
    if state["next"] != n:
        emit({**out, "summary": "boundary mismatch"}, 5)
    try:
        manifest = load_manifest()
        instructions, tools = build_agent(manifest)
    except Exception as e:  # noqa: BLE001 - any failure to build the agent is an agent error
        emit({**out, "summary": f"agent error: {type(e).__name__}: {short(e, 200)}"}, 3)
    if not state["messages"]:
        state["messages"] = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(req["task"])},
        ]
    (sdir / f"{n}.started").write_text(str(time.time()))
    last = state["messages"][-1]

    if last["role"] == "assistant" and last.get("tool_calls"):
        call = last["tool_calls"][0]
        name, result, rc = run_tool(call, tools)
        content = json.dumps(bounded(result, MAX_TOOL_RESULT))  # valid JSON after truncation
        state["messages"].append(
            {"role": "tool", "tool_call_id": call.get("id"), "content": content}
        )
        keys = list(result)[:3] if isinstance(result, dict) else "ok"
        out.update(kind="tool", tool=name, summary=f"{name} -> {keys}")
    else:
        rc = run_model(state, manifest, tools, out)

    state["next"] = n + 1
    write_private(state_file, json.dumps(state))  # atomic: a crash leaves the previous state
    (sdir / f"{n}.done").write_text(str(time.time()))
    emit(out, rc)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 - unknown outcome; never print a message that may hold a token
        print(f"agent_runtime.step: unexpected {type(e).__name__}", file=sys.stderr)
        sys.exit(1)
