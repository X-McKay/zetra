"""Bounded process RSS/high-water observations at trusted exec boundaries.

The kernel high-water mark captures peaks between periodic samples. These are
process-wide facts, not per-session allocations; qualification must run alone.
Missing observations are explicit and do not interrupt production cleanup.
"""

import os
import time
from pathlib import Path

from runner import records

SAMPLE_SECONDS = 5.0
LIMIT_PATHS = (
    Path("/sys/fs/cgroup/memory.max"),
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)


def process_memory(proc=Path("/proc/self"), limits=LIMIT_PATHS, *, hostname=None):
    values = {}
    for line in (proc / "status").read_text().splitlines():
        name, _, value = line.partition(":")
        if name in ("VmRSS", "VmHWM"):
            number, unit = value.split()
            if unit != "kB":
                raise ValueError("unknown process memory unit")
            values[name] = int(number) * 1024
    stat = (proc / "stat").read_text()
    pid, start = stat.split(" ", 1)[0], stat.rsplit(")", 1)[1].split()[19]
    limit = None
    for path in limits:
        try:
            text = path.read_text().strip()
        except FileNotFoundError:
            continue
        if text.isdecimal() and 0 < int(text) < 1 << 60:
            limit = int(text)
            break
    if not limit or not 0 < values["VmRSS"] <= values["VmHWM"]:
        raise ValueError("missing finite memory limit or process memory")
    host = hostname or os.environ.get("HOSTNAME") or os.uname().nodename
    return {
        "process_id": f"{host}:{pid}:{start}",
        "rss_bytes": values["VmRSS"],
        "hwm_bytes": values["VmHWM"],
        "limit_bytes": limit,
    }


class MemoryAudit:
    def __init__(self, boundary):
        self.boundary = boundary
        self.started = time.monotonic()
        self.last = float("-inf")

    def sample(self, phase):
        now = time.monotonic()
        if phase == "periodic" and now - self.last < SAMPLE_SECONDS:
            return
        self.last = now
        try:
            facts = process_memory()
            error = None
        except (OSError, ValueError, KeyError, IndexError) as exc:
            facts = dict.fromkeys(("process_id", "rss_bytes", "hwm_bytes", "limit_bytes"))
            error = type(exc).__name__
        records.emit(
            "boundary_memory",
            session_id=self.boundary.inp.session_id,
            generation=self.boundary.generation,
            boundary=self.boundary.boundary,
            phase=phase,
            sampled_at=time.time(),
            elapsed_ms=(now - self.started) * 1000,
            error=error,
            **facts,
        )
