"""Landlock rules applied before any agent code runs (spec 11.6).

Depth, not the wall: the VM and the NetworkPolicy are the boundary, so a missing module or
an unsupported kernel only logs a warning.
"""

import logging
import os
import sys

log = logging.getLogger("agent_runtime.confine")

READ_ONLY = ["/usr", "/lib", "/lib64", "/etc", "/var/run/secrets/agent-identity"]


def paths() -> tuple[list[str], list[str]]:
    app = os.environ.get("AGENT_APP_DIR", "/app")
    sandbox = os.environ.get("AGENT_SANDBOX_DIR", "/sandbox")
    # The interpreter's own prefixes are read-only too, wherever the base image put them.
    ro = [app, *READ_ONLY, sys.prefix, sys.base_prefix]
    rw = [sandbox, "/tmp"]
    exists = os.path.exists
    return sorted({p for p in ro if exists(p)} - set(rw)), [p for p in rw if exists(p)]


def apply() -> bool:
    try:
        import landlock
    except ImportError:
        log.warning("landlock module unavailable; continuing without in-guest confinement")
        return False
    ro, rw = paths()
    try:
        rs = landlock.Ruleset()
        read = landlock.FSAccess.READ_FILE | landlock.FSAccess.READ_DIR | landlock.FSAccess.EXECUTE
        rs.allow(*ro, rules=read)
        rs.allow(*rw)
        rs.apply()
    except Exception as e:  # noqa: BLE001 - any failure is a warning by design
        log.warning("landlock not applied: %s", type(e).__name__)
        return False
    return True
