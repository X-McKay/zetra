"""Data-only review candidates; observations never authorize access."""

import json
from pathlib import Path


def profile(path):
    path = Path(path)
    if path.stat().st_size > 20_000_000:
        raise ValueError("profile too large")
    observations, denied = [], []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict) or set(event) != {
            "agent",
            "image",
            "kind",
            "target",
            "outcome",
        }:
            raise ValueError("expected scoped normalized event")
        if event["outcome"] not in ("allowed", "observed", "denied"):
            raise ValueError("invalid observation outcome")
        if event["kind"] not in ("read", "write", "connect", "exec"):
            raise ValueError("invalid observation kind")
        (denied if event["outcome"] == "denied" else observations).append(event)
    scopes = {(e["agent"], e["image"]) for e in observations + denied}
    if len(scopes) != 1:
        raise ValueError("profile requires one exact agent/image scope")
    from .manifest import Release

    Release.immutable_image(next(iter(scopes))[1])
    return {
        "status": "REVIEW_CANDIDATE",
        "authorizes": False,
        "observations": observations,
        "denied": denied,
    }
