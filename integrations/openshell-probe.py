#!/usr/bin/env python3
"""Independent actual sandbox probe; no models, keys, or sensitive input."""

import errno
import json
import os
import socket
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

checks = []


def check(name, passed, **detail):
    checks.append({"name": name, "status": "passed" if passed else "failed", "detail": detail})


scratch = Path("/tmp/zetra-approved-scratch")
scratch.write_text("safe test fixture")
check("scratch-write-read", scratch.read_text() == "safe test fixture")
scratch.unlink()

for name, path, mode in [
    ("outside-read-denied", "/var/lib/dpkg/status", "r"),
    ("outside-write-denied", "/var/tmp/zetra-forbidden-write", "w"),
    ("etc-write-denied", "/etc/zetra-forbidden-write", "w"),
]:
    target = Path(path)
    metadata = os.stat(path if mode == "r" else target.parent)
    try:
        with target.open(mode) as handle:
            if mode == "r":
                handle.read(1)
            else:
                handle.write("unexpected write")
        check(name, False, errno=None, mode=oct(stat.S_IMODE(metadata.st_mode)), path=path)
        if mode == "w":
            target.unlink(missing_ok=True)
    except OSError as error:
        check(
            name,
            error.errno in (errno.EACCES, errno.EPERM),
            errno=error.errno,
            exception=type(error).__name__,
            mode=oct(stat.S_IMODE(metadata.st_mode)),
            path=path,
        )

try:
    with urllib.request.urlopen("http://example.com", timeout=5) as response:
        response.read(1)
    check("unapproved-http-denied", False)
except Exception as error:
    permission_denied = (
        isinstance(error, urllib.error.URLError)
        and isinstance(error.reason, OSError)
        and error.reason.errno in (errno.EACCES, errno.EPERM)
    )
    check(
        "unapproved-http-denied",
        permission_denied
        or (isinstance(error, urllib.error.HTTPError) and error.code in (403, 407)),
        exception=type(error).__name__,
        httpStatus=getattr(error, "code", None),
        reason=str(error)[:200],
    )

try:
    with socket.create_connection(("1.1.1.1", 443), timeout=5):
        pass
    check("direct-egress-denied", False)
except OSError as error:
    check(
        "direct-egress-denied",
        error.errno in (errno.EACCES, errno.EPERM),
        errno=error.errno,
        exception=type(error).__name__,
        reason=str(error)[:200],
    )

# Execute actual package factory/runtime copies uploaded by the reproduction script.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


def execute_reference_agent():
    from knowledge.factory import create_agent

    from zetra.runtime import RuntimeDependencies

    deps = RuntimeDependencies(
        frozenset({"knowledge.read"}),
        8,
        30,
        0.1,
        Path("/tmp/zetra-no-stop-file"),
        knowledge={"zetra": "Zetra means ZEro-TRust Agents."},
    )
    return create_agent(deps).run({"key": "zetra"})


answer = execute_reference_agent()
check(
    "actual-offline-reference-agent",
    answer.get("answer") == "Zetra means ZEro-TRust Agents.",
    result=answer,
)

report = {
    "scope": "actual-OpenShell-supervised-Python-sandbox",
    "uid": os.getuid(),
    "cwd": os.getcwd(),
    "checks": checks,
    "status": "passed" if all(c["status"] == "passed" for c in checks) else "failed",
    "limitations": [
        "No principal authentication or production credential exchange test",
        "etc write denial alone may also be DAC; outside read/write use world-accessible locations",
        "No automatic permission expansion or business authorization is implied",
    ],
}
print(json.dumps(report, indent=2))
raise SystemExit(0 if report["status"] == "passed" else 1)
