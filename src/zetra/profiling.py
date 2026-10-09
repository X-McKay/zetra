"""Normalize scoped recorded events into a review candidate, not authorization.

Explicit denied outcomes are retained separately and never proposed as access.
An absent outcome means observational evidence, not verified authorization.
"""

from __future__ import annotations

import json
from pathlib import Path

from .deployment import immutable_image
from .manifest import ContractError


def profile(path: Path, agent: str, image: str) -> dict:
    immutable_image(image)
    if not agent or path.stat().st_size > 20_000_000:
        raise ContractError("Agent required; fixture must be <= 20 MB")
    processes, files, network = set(), set(), set()
    accepted, rejected = 0, 0
    denied_count = 0
    denied = set()

    def unique(pairs):
        event = {}
        for key, value in pairs:
            if key in event:
                raise ContractError(f"Duplicate event key: {key}")
            event[key] = value
        return event

    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line, object_pairs_hook=unique)
        except json.JSONDecodeError as exc:
            raise ContractError(f"Invalid event JSON at line {line_number}") from exc
        if not isinstance(event, dict):
            raise ContractError(f"Event must be an object at line {line_number}")
        if (
            event.get("agent") != agent
            or event.get("image") != image
            or event.get("container") != "agent"
        ):
            rejected += 1
            continue
        kind = event.get("kind")
        outcome = event.get("outcome", "observed")
        if not isinstance(outcome, str) or outcome not in {"allowed", "denied", "observed"}:
            raise ContractError(f"Unsupported normalized outcome at line {line_number}")
        if (
            kind == "exec"
            and isinstance(event.get("binary"), str)
            and event["binary"].startswith("/")
        ):
            observation = {"kind": "exec", "binary": event["binary"]}
            if outcome != "denied":
                processes.add(event["binary"])
        elif (
            kind == "file"
            and isinstance(event.get("path"), str)
            and event["path"].startswith("/")
            and event.get("mode") in {"read", "write"}
        ):
            observation = {"kind": "file", "path": event["path"], "mode": event["mode"]}
            if outcome != "denied":
                files.add((event["path"], event["mode"]))
        elif (
            kind == "connect"
            and isinstance(event.get("address"), str)
            and type(event.get("port")) is int
            and 1 <= event["port"] <= 65535
        ):
            observation = {"kind": "connect", "address": event["address"], "port": event["port"]}
            if outcome != "denied":
                network.add((event["address"], event["port"]))
        else:
            raise ContractError(f"Unsupported or malformed normalized event at line {line_number}")
        if outcome == "denied":
            denied_count += 1
            denied.add(json.dumps(observation, sort_keys=True))
        accepted += 1
    if not accepted:
        raise ContractError("No events match exact agent/image/container scope")
    return {
        "apiVersion": "zetra.dev/profile-v1alpha1",
        "agent": agent,
        "image": image,
        "status": "review-candidate",
        "authorizes": False,
        "matchedEvents": accepted,
        "excludedEvents": rejected,
        "deniedEvents": denied_count,
        "deniedObservations": [json.loads(item) for item in sorted(denied)],
        "processes": sorted(processes),
        "files": [{"path": p, "mode": m} for p, m in sorted(files)],
        "network": [{"address": a, "port": p} for a, p in sorted(network)],
        "limitations": [
            "Normalized fixtures are not raw Tetragon events; vendor adapter and trusted capture required",
            "Observed behavior may be malicious or incomplete; never add observed access automatically",
            "Denied observations are excluded from access candidates; missing outcomes remain observations, not proof of authorization",
            "IP observations cannot authorize domain names, dynamic endpoints or future workloads",
        ],
    }
