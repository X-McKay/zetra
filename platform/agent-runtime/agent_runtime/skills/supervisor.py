"""Starts one skill script, holds it to its limits and ends its whole process tree (section
9.4 of `platform/registry/contract/skills.md`).

    python -m agent_runtime.skills.supervisor

The launcher writes one JSON request on standard input and reads one JSON answer on standard
output. The supervisor starts the script and does nothing else. It is the reaper of every
process the script leaves behind, so the script's tree is exactly the supervisor's
descendants, and it signals no process that it has not found to be one of them.
"""

from __future__ import annotations

import ctypes
import json
import os
import select
import signal
import subprocess
import sys
import time

PR_SET_PDEATHSIG = 1
PR_SET_CHILD_SUBREAPER = 36

SAMPLE_SECONDS = 0.02  # how often memory and tasks are sampled
END_SECONDS = 5.0  # how long ending the tree may take before the run is a failure
DRAIN_SECONDS = 1.0  # how long output is read once the tree is ended
STOPPED = ("T", "t")
GONE = ("Z", "X", "x")

_ended = False  # a signal asked for the end of the run


def _on_signal(signum, frame) -> None:
    global _ended
    _ended = True


def _prctl(option: int, value: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), f"prctl({option}) failed")


def _stat(pid: int) -> tuple[str, int, int] | None:
    """(state, parent pid, start time) of a process, or None when it is gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            text = handle.read().decode("ascii", "replace")
    except OSError:
        return None
    fields = text[text.rfind(")") + 2 :].split()
    try:
        return fields[0], int(fields[1]), int(fields[19])
    except (IndexError, ValueError):
        return None


class Tree:
    """The supervisor's descendants, found through `/proc`."""

    def __init__(self) -> None:
        self.me = os.getpid()
        mine = _stat(self.me)
        if mine is None:
            raise OSError("cannot read /proc for the supervisor")
        self.start = mine[2]

    def scan(self) -> dict[int, tuple[str, int, int]]:
        """Every descendant by pid: a walk down from the supervisor over every process in
        `/proc`. A child must not have started before its parent, which keeps a reused pid
        from joining the tree."""
        table: dict[int, tuple[str, int, int]] = {}
        for name in os.listdir("/proc"):
            if name.isdigit():
                info = _stat(int(name))
                if info is not None:
                    table[int(name)] = info
        children: dict[int, list[int]] = {}
        for pid, (_, parent, _) in table.items():
            children.setdefault(parent, []).append(pid)
        found: dict[int, tuple[str, int, int]] = {}
        pending = [(self.me, self.start)]
        while pending:
            parent, parent_start = pending.pop()
            for pid in children.get(parent, ()):
                info = table[pid]
                if pid in found or pid == self.me or info[2] < parent_start:
                    continue
                found[pid] = info
                pending.append((pid, info[2]))
        return found

    def sample(self) -> tuple[int, int]:
        """(bytes of anonymous and shared memory, tasks) summed over the tree. Walks the
        `children` files, which is cheap and good enough for a sample; falls back to a scan
        where the kernel has no such files."""
        try:
            pids = self._walk()
        except OSError:
            pids = list(self.scan())
        memory = tasks = 0
        for pid in pids:
            try:
                with open(f"/proc/{pid}/status", "rb") as handle:
                    lines = handle.read().decode("ascii", "replace").splitlines()
            except OSError:
                continue
            for line in lines:
                key, _, value = line.partition(":")
                if key in ("RssAnon", "RssShmem"):
                    memory += int(value.split()[0]) * 1024
                elif key == "Threads":
                    tasks += int(value)
        return memory, tasks

    def _walk(self) -> list[int]:
        found: list[int] = []
        pending = [self.me]
        while pending:
            pid = pending.pop()
            try:
                threads = os.listdir(f"/proc/{pid}/task")
            except FileNotFoundError:
                continue
            for tid in threads:
                try:
                    with open(f"/proc/{pid}/task/{tid}/children", "rb") as handle:
                        kids = [int(k) for k in handle.read().split()]
                except FileNotFoundError:
                    if pid == self.me:
                        raise
                    continue
                for kid in kids:
                    if kid not in found and kid != self.me:
                        found.append(kid)
                        pending.append(kid)
        return found

    def signal(self, pid: int, start: int, signum: int, tree: dict) -> None:
        """Send `signum` to `pid` only if it is still the descendant the scan found: the
        process is pinned with a pidfd first, then its start time and parent are read again."""
        if pid <= 1 or pid in (self.me, os.getppid()):
            return
        try:
            handle = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        except (AttributeError, OSError):
            handle = None  # no pidfd here: the check below and the kill are not atomic
        try:
            info = _stat(pid)
            if info is None or info[2] != start or info[0] in GONE:
                return
            if info[1] != self.me and info[1] not in tree:
                return
            if handle is not None:
                signal.pidfd_send_signal(handle, signum)
            else:
                os.kill(pid, signum)
        except ProcessLookupError:
            pass
        finally:
            if handle is not None:
                os.close(handle)


def _reap(script: int, status: list[int]) -> None:
    """Reap every child that has exited; keep the script's own status."""
    while True:
        try:
            pid, value = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return
        if pid == script:
            status.append(os.waitstatus_to_exitcode(value))


def _end(tree: Tree, script: int, status: list[int]) -> bool:
    """End every descendant: stop each until a pass finds none running, kill them all, reap,
    and repeat until none is left. False when one remains after `END_SECONDS`."""
    deadline = time.monotonic() + END_SECONDS
    while True:
        _reap(script, status)
        found = tree.scan()
        if not found:
            return True
        if time.monotonic() > deadline:
            return False
        running = {p: i for p, i in found.items() if i[0] not in STOPPED + GONE}
        if running:
            for pid, info in running.items():
                tree.signal(pid, info[2], signal.SIGSTOP, found)
        else:
            for pid, info in found.items():
                tree.signal(pid, info[2], signal.SIGKILL, found)
        time.sleep(0.005)


def _read(fd: int, kept: bytearray, bound: int) -> bool:
    """Read what is there into `kept`, keeping one byte past the bound so that going over is
    seen. False at the end of the stream."""
    try:
        data = os.read(fd, 65536)
    except BlockingIOError:
        return True
    if not data:
        return False
    room = bound + 1 - len(kept)
    if room > 0:
        kept += data[:room]
    return True


def supervise(request: dict) -> dict:
    tree = Tree()
    out, err = bytearray(), bytearray()
    bound = int(request["output_bytes"])
    try:
        process = subprocess.Popen(
            request["argv"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=request["cwd"],
            env=request["env"],
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        return {"started": False, "error": str(exc), "clean": not tree.scan()}
    deadline = time.monotonic() + int(request["timeout_seconds"])
    streams = {process.stdout.fileno(): out, process.stderr.fileno(): err}
    for fd in streams:
        os.set_blocking(fd, False)
    status: list[int] = []
    hit: set[str] = set()
    next_sample = 0.0
    while not _ended:
        now = time.monotonic()
        if now >= deadline:
            hit.add("time")
        if now >= next_sample:
            memory, tasks = tree.sample()
            if memory > int(request["memory_bytes"]):
                hit.add("memory")
            if tasks > int(request["processes"]):
                hit.add("processes")
            next_sample = now + SAMPLE_SECONDS
        if len(out) > bound or len(err) > bound:
            hit.add("output")
        if hit:
            break
        _reap(process.pid, status)
        if status:
            break
        wait = max(0.0, min(next_sample, deadline) - time.monotonic())
        if streams:
            ready, _, _ = select.select(list(streams), [], [], wait)
            for fd in ready:
                if not _read(fd, streams[fd], bound):
                    del streams[fd]
        else:
            time.sleep(wait)
    for fd in list(streams):
        while _read(fd, streams[fd], bound) and len(streams[fd]) <= bound:
            if not select.select([fd], [], [], 0)[0]:
                break
    clean = _end(tree, process.pid, status)
    drain_until = time.monotonic() + DRAIN_SECONDS
    while streams and time.monotonic() < drain_until:
        ready, _, _ = select.select(list(streams), [], [], drain_until - time.monotonic())
        for fd in ready:
            if not _read(fd, streams[fd], bound):
                del streams[fd]
    process.stdout.close()
    process.stderr.close()
    if status:
        process.returncode = status[0]
    if len(out) > bound or len(err) > bound:
        hit.add("output")
    stopped = bool(hit) or _ended or not status
    return {
        "started": True,
        "clean": clean,
        "exit_status": None if stopped else status[0],
        "limits_hit": sorted(hit),
        "signalled": _ended,
        "stdout": bytes(out[:bound]).decode("utf-8", "replace"),
        "stderr": bytes(err[:bound]).decode("utf-8", "replace"),
    }


def main() -> int:
    # Signals first, so that a run asked to end before the script starts ends cleanly.
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, _on_signal)
    _prctl(PR_SET_CHILD_SUBREAPER, 1)
    _prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    request = json.loads(sys.stdin.buffer.read())
    if os.getppid() != request["parent"]:
        return 1  # the step died before the parent-death signal was asked for
    if _ended:  # asked to end before the script started: nothing ran
        answer = {"started": True, "clean": True, "exit_status": None, "limits_hit": []}
        answer.update(signalled=True, stdout="", stderr="")
    else:
        answer = supervise(request)
    try:
        os.write(1, json.dumps(answer).encode())
    except OSError:
        return 1  # nobody is reading: the step is gone
    return 0


if __name__ == "__main__":
    sys.exit(main())
