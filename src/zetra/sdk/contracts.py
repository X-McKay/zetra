"""Contract base model and the platform-stamped run dependencies."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    """Strict, frozen model for inputs, outputs and write payloads.

    Unknown fields are rejected and values are not coerced, so a payload a human
    approves is exactly the payload the receiver commits.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class RunDeps(BaseModel):
    """Stamped by the runtime, never from caller JSON. Copied into every activity, so kept small."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run: str
    release: str
    principal: str
    deadline: datetime | None = None
    input: dict[str, Any]


def canonical(value: Any) -> str:
    """Canonical JSON for digests. RFC 8785 JCS replaces this in a later layer."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()
