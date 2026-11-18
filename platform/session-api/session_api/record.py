"""The record line (`contract/sessions.md` section 5): one JSON line on standard output.

`write` is the only place it is written. It is given the fields the line holds and nothing
else, so a task, a report or a token cannot get into it.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class Entry:
    route: str
    caller: str | None = None
    role: str | None = None
    agent: str | None = None
    release: str | None = None
    session_id: str | None = None
    purpose: str | None = None
    code: str = "ok"  # the error code of the answer, or "ok"


def write(entry: Entry, now: datetime) -> None:
    line = {
        "time": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "route": entry.route,
        "caller": entry.caller,
        "role": entry.role,
        "agent": entry.agent,
        "release": entry.release,
        "sessionId": entry.session_id,
        "purpose": entry.purpose,
        "code": entry.code,
    }
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    sys.stdout.flush()
