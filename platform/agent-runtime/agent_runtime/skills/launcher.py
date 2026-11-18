"""Runs one script of a skill under the controls of section 9 of
`platform/registry/contract/skills.md`.

This file is the interface between the step, which offers `skill_run` to the model, and
the code that starts and supervises the process. The step calls `run` and nothing else.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.skills import format as fmt

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
    started = time.monotonic()
    known = {"skill": None, "script": None, "digest": None}
    args = _arguments(arguments)

    def record(refused: str | None, **outcome: object) -> Run:
        return Run(
            skill=known["skill"],
            script=known["script"],
            digest=known["digest"],
            arguments=args or (),
            refused=refused,
            exit_status=outcome.get("exit_status"),
            limits_hit=tuple(outcome.get("limits_hit", ())),
            stdout=outcome.get("stdout", ""),
            stderr=outcome.get("stderr", ""),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        request, timeout = _prepare(Path(app), Path(workspace), listed, skill, script, args, known)
    except _Refused as refusal:
        return record(refusal.code)
    answer = _supervise(request, limits or Limits(timeout_seconds=timeout))
    if not answer.get("started"):
        return record("interpreter")  # the interpreter could not be started
    return record(
        None,
        exit_status=answer["exit_status"],
        limits_hit=answer["limits_hit"],
        stdout=answer["stdout"],
        stderr=answer["stderr"],
    )


# How long the launcher waits for the supervisor beyond the script's own timeout: the time
# the supervisor may take to end the tree and read the last output, and some slack.
_SUPERVISOR_SLACK_SECONDS = 15


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


def _arguments(arguments: object) -> tuple[str, ...] | None:
    """The arguments when they are a list of strings within the bounds of 9.2, else None."""
    if not isinstance(arguments, list | tuple) or len(arguments) > MAX_ARGUMENTS:
        return None
    total = 0
    kept = []
    for argument in arguments:
        if not isinstance(argument, str) or "\x00" in argument:
            return None
        try:
            size = len(argument.encode("utf-8"))
        except UnicodeEncodeError:
            return None
        if size > MAX_ARGUMENT_BYTES:
            return None
        total += size
        kept.append(str(argument))
    if total > MAX_ARGUMENTS_BYTES:
        return None
    return tuple(kept)


def _confined(directory: Path, relative: str) -> Path:
    """The script's path, when it is a regular file of the skill directory reached through
    no link at any step. Refuses with `path` otherwise."""
    target = directory
    for part in relative.split("/"):
        if part in ("", ".", ".."):
            raise _Refused("path")
        target = target / part
        try:
            mode = target.lstat().st_mode
        except OSError:
            raise _Refused("path") from None
        if stat.S_ISLNK(mode):
            raise _Refused("path")
    if not stat.S_ISREG(mode):
        raise _Refused("path")
    if os.path.realpath(target) != os.path.join(os.path.abspath(directory), relative):
        raise _Refused("path")
    return Path(os.path.abspath(target))


def _prepare(
    app: Path,
    workspace: Path,
    listed: Sequence[str],
    skill: object,
    script: object,
    args: tuple[str, ...] | None,
    known: dict,
) -> tuple[dict, int]:
    """Every check of 9.2 and 9.3 in order. Returns what the supervisor is to run and the
    script's own timeout, or raises `_Refused`."""
    if not sys.platform.startswith("linux") or not os.path.isdir("/proc/self"):
        raise _Refused("unsupported")
    names = [name for name in listed if isinstance(name, str)]
    if not isinstance(skill, str) or not fmt.is_name(skill) or skill not in names:
        raise _Refused("unknown_skill")
    known["skill"] = skill
    directory = app / "skills" / skill
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise _Refused("path")
        fmt.files(directory)  # a link or a special file anywhere in the skill
    except (OSError, fmt.SkillError):
        raise _Refused("path") from None
    try:
        sidecar = fmt.sidecar(directory)
    except fmt.SkillError:
        raise _Refused("digest") from None  # it read when the agent was loaded
    if sidecar is None:
        raise _Refused("undeclared")
    declared = sidecar.script(script) if isinstance(script, str) else None
    if declared is None:
        raise _Refused("unknown_script")
    known["script"] = declared.path
    path = _confined(directory, declared.path)
    if args is None:
        raise _Refused("arguments")
    try:
        if not stat.S_ISDIR(workspace.lstat().st_mode):
            raise _Refused("workspace")
    except OSError:
        raise _Refused("workspace") from None
    interpreter = shutil.which(declared.interpreter, path=INTERPRETER_PATH)
    if interpreter is None or not os.path.isabs(interpreter):
        raise _Refused("interpreter")
    try:
        pinned = fmt.read_lock(app / fmt.LOCK).get(skill)
        known["digest"] = fmt.digest(directory)
    except (OSError, fmt.SkillError):
        raise _Refused("digest") from None
    if pinned is None or pinned["digest"] != known["digest"]:
        raise _Refused("digest")
    request = {
        "argv": [interpreter, str(path), *args],
        "cwd": os.path.abspath(workspace),
        "env": {
            "PATH": INTERPRETER_PATH,
            "HOME": os.path.abspath(workspace),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "parent": os.getpid(),
    }
    return request, declared.timeout_seconds


def _supervise(request: dict, limits: Limits) -> dict:
    """Start the supervisor, hand it the request and wait for its answer. Raises
    `LauncherFailure` unless the answer says the script's tree is gone."""
    request = {
        **request,
        "timeout_seconds": limits.timeout_seconds,
        "output_bytes": limits.output_bytes,
        "memory_bytes": limits.memory_bytes,
        "processes": limits.processes,
    }
    try:
        supervisor = subprocess.Popen(
            [sys.executable, "-P", "-m", "agent_runtime.skills.supervisor"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            close_fds=True,
        )
    except OSError as exc:
        raise LauncherFailure(f"the supervisor could not be started: {exc}") from exc
    wait = limits.timeout_seconds + _SUPERVISOR_SLACK_SECONDS
    try:
        out, err = supervisor.communicate(json.dumps(request).encode(), timeout=wait)
    except subprocess.TimeoutExpired:
        supervisor.kill()
        supervisor.wait()
        raise LauncherFailure("the supervisor did not answer in time") from None
    except BaseException:
        # The step is being interrupted: the supervisor ends the tree on SIGTERM.
        supervisor.terminate()
        try:
            supervisor.wait(timeout=_SUPERVISOR_SLACK_SECONDS)
        except subprocess.TimeoutExpired:
            supervisor.kill()
            supervisor.wait()
        raise
    try:
        answer = json.loads(out) if supervisor.returncode == 0 else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or answer.get("clean") is not True:
        detail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no answer"]
        raise LauncherFailure(f"the script's processes may remain: {detail[0]}")
    return answer
