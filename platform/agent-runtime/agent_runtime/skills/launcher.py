"""Runs one script of a skill under the controls of section 9 of
`platform/registry/contract/skills.md`.

This file is the interface between the step, which offers `skill_run` to the model, and
the code that starts and supervises the process. The step calls `run` and nothing else.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# Where a script's interpreter is looked for, and the PATH the script is given.
INTERPRETER_PATH = "/usr/local/bin:/usr/bin:/bin"

MAX_ARGUMENTS = 32
MAX_ARGUMENT_BYTES = 4096
MAX_ARGUMENTS_BYTES = 16384  # all arguments together

# Why a script was not started (section 9.3).
REFUSALS = (
    "unknown_skill",
    "undeclared",
    "unknown_script",
    "path",
    "arguments",
    "interpreter",
    "digest",
    "workspace",
    "unsupported",
)
# What can stop a started script (section 9.4).
LIMITS = ("memory", "output", "processes", "time")


class LauncherFailure(RuntimeError):
    """The launcher cannot say that a script's processes are gone. The step must end with
    a status the runner does not know, so that the sandbox is replaced."""


@dataclass(frozen=True)
class Limits:
    timeout_seconds: int  # from the sidecar
    output_bytes: int = 16384  # per stream
    memory_bytes: int = 536870912  # 512 MiB, summed over the script's process tree
    processes: int = 32  # tasks in the tree; threads count


@dataclass(frozen=True)
class Run:
    """What one call did. `refused` set means no process was started."""

    skill: str | None  # None when the name given was not a skill the agent lists
    script: str | None  # None when the name given was not a script the skill lists
    digest: str | None  # of the skill directory as it was run
    arguments: tuple[str, ...]  # in full; () when they were refused
    refused: str | None  # one of REFUSALS
    exit_status: int | None  # None when refused or stopped; negative N: ended by signal N
    limits_hit: tuple[str, ...]  # of LIMITS, sorted
    stdout: str
    stderr: str
    duration_ms: int


def run(
    app: Path,
    workspace: Path,
    listed: Sequence[str],
    skill: object,
    script: object,
    arguments: object,
    *,
    limits: Limits | None = None,
) -> Run:
    """Run `script` of `skill` with `arguments`, or refuse.

    `app` is the app directory (skills under `app/skills`, the lock at `app/skills.lock`),
    `workspace` the session's workspace and `listed` the skill names of the manifest.
    `skill`, `script` and `arguments` are what the model sent, unchecked. `limits` replaces
    the bounds for a test; left out, they are the contract's with the script's own timeout.

    Never raises for anything the model sent. Raises `LauncherFailure` when it cannot say
    the script's processes are gone.
    """
    raise NotImplementedError("package 4.3")
