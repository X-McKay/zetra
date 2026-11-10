"""One boundary of `zetra-agents run`: the SDK's own `agent_runtime.step`, with the two
things a developer's machine does not have replaced before it starts. The gateway's MCP
route becomes the scenario's scripted replies behind the Agent's grants, and the model
route becomes the scenario's scripted turns or, with `--live`, an OpenAI-compatible
endpoint named by the environment. Everything else is the code an image runs: the
length-prefixed request, the state file, tool dispatch, bounds and exit codes.

Development tooling; never in an image.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

SCRIPT_ENDED = 64  # outside the step protocol's 0 to 5
SPEC_ENV = "ZETRA_AGENTS_LOCAL_RUN"
MODEL_URL = "ZETRA_AGENTS_MODEL_URL"
MODEL_NAME = "ZETRA_AGENTS_MODEL_NAME"
MODEL_API_KEY = "ZETRA_AGENTS_MODEL_API_KEY"


class ScriptEnded(Exception):
    """The agent asked the scripted model for a turn the scenario does not have."""


def scripted_client(turns: list[dict]):
    """The scenario's turns, one per model call, in the response shape the step reads."""

    def create(*, messages, **_):
        index = sum(1 for m in messages if m["role"] == "assistant")
        if index >= len(turns):
            raise ScriptEnded(
                f"the scenario scripts {len(turns)} model turns and the session asked for "
                f"turn {index + 1}; add a turn, or end the script with `say:`"
            )
        turn = turns[index]
        if "say" in turn:
            message = NS(content=turn["say"], tool_calls=None)
        else:
            function = NS(name=turn["call"], arguments=json.dumps(turn.get("arguments") or {}))
            message = NS(content="", tool_calls=[NS(id=f"call_{index}", function=function)])
        usage = NS(prompt_tokens=0, completion_tokens=0)
        return NS(choices=[NS(message=message)], usage=usage)

    return NS(chat=NS(completions=NS(create=create)))


def live_client(settings: dict):
    """An OpenAI-compatible endpoint. The manifest's model name is the platform route's;
    `ZETRA_AGENTS_MODEL_NAME` says what the local endpoint calls it."""
    from openai import OpenAI

    client = OpenAI(
        base_url=settings[MODEL_URL],
        api_key=settings.get(MODEL_API_KEY) or "none",
        max_retries=0,
    )
    name = settings.get(MODEL_NAME)

    def create(**kwargs):
        if name:
            kwargs["model"] = name
        return client.chat.completions.create(**kwargs)

    return NS(chat=NS(completions=NS(create=create)))


def scripted_mcp(spec: dict):
    """`FakeMcp` behind the Agent's grants, with every call appended to the run's log."""
    from agent_runtime.gateway import PolicyDenied

    from zetra_agents.testing import FakeMcp, UnscriptedCall

    fake = FakeMcp({b: set(t) for b, t in spec["grants"].items()}, agent=spec["agent"])
    for reply in spec["mcp"]:
        result = RuntimeError(reply["error"]) if "error" in reply else reply["result"]
        fake.reply(reply["tool"], result, backend=reply.get("backend"))
    log = Path(spec["mcpLog"])

    def record(backend: str, tool: str, arguments: dict, outcome: str) -> None:
        entry = {"backend": backend, "tool": tool, "arguments": arguments, "outcome": outcome}
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    async def call_tool(backend: str, tool: str, arguments: dict, timeout: float = 25.0) -> dict:
        outcome = "invalid"  # refused before it would have left the sandbox
        before = len(fake.calls)
        try:
            result = await fake(backend, tool, arguments, timeout)
            outcome = "ok"
            return result
        except PolicyDenied:
            outcome = "denied"
            raise
        except UnscriptedCall:
            outcome = "unscripted"
            raise
        except Exception:
            if len(fake.calls) > before:
                outcome = "error"
            raise
        finally:
            record(backend, tool, arguments, outcome)

    return call_tool


def main() -> None:
    spec = json.loads(Path(os.environ[SPEC_ENV]).read_text(encoding="utf-8"))
    # The endpoint and its key are this shim's. Out of os.environ before agent code runs,
    # so a tool that records its environment, or a process it starts, does not carry
    # them. That is hygiene, not isolation: this process's memory still holds them.
    settings = {
        name: os.environ.pop(name)
        for name in (MODEL_URL, MODEL_NAME, MODEL_API_KEY)
        if name in os.environ
    }
    from agent_runtime import confine, mcp, models, step

    confine.apply = lambda: False  # a host path layout is not the image's; nothing to confine to
    mcp.call_tool = scripted_mcp(spec)
    turns = spec["model"]
    models.chat_client = (
        (lambda: live_client(settings)) if turns is None else (lambda: scripted_client(turns))
    )
    try:
        step.main()
    except ScriptEnded as exc:
        print(f"zetra-agents run: {exc}", file=sys.stderr)
        sys.exit(SCRIPT_ENDED)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 - as the SDK's driver: a message may hold a token
        print(f"zetra-agents run: unexpected {type(e).__name__}", file=sys.stderr)
        sys.exit(1)
