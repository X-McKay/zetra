"""Release signature verification (6.4 AgentRelease step 1). Fails closed."""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import time
from typing import Protocol

log = logging.getLogger(__name__)


class SignatureVerifier(Protocol):
    def verify(self, image: str, manifest_sha256: str, policy_sha256: str) -> bool: ...


class CosignVerifier:
    """`cosign verify` against the organisation key.

    homelab profile: the image signature only; the policy digest is recomputed by the
    controller itself (step 2). enterprise profile: additionally `cosign verify-attestation`,
    whose predicate must carry `manifestSha256` and `policySha256` equal to the spec.
    Any error, timeout or unexpected output is a failed verification.
    """

    def __init__(
        self,
        key_path: str,
        profile: str = "homelab",
        predicate_type: str = "https://agentplatform.io/attestations/release/v1",
        cosign: str = "cosign",
        timeout: float = 60.0,
        cache_seconds: float = 600.0,
        run=subprocess.run,
    ) -> None:
        if profile not in ("homelab", "enterprise"):
            raise ValueError("profile must be homelab or enterprise")
        self._key = key_path
        self._profile = profile
        self._type = predicate_type
        self._cosign = cosign
        self._timeout = timeout
        self._run = run
        self._cache_seconds = cache_seconds
        self._verified: dict[tuple[str, str, str], float] = {}  # positive results only

    def _cosign_ok(self, *args: str) -> str | None:
        try:
            proc = self._run(
                [self._cosign, *args],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("cosign could not run: %s", type(exc).__name__)
            return None
        if proc.returncode != 0:
            log.info("cosign %s refused the image (exit %d)", args[0], proc.returncode)
            return None
        return proc.stdout

    def verify(self, image: str, manifest_sha256: str, policy_sha256: str) -> bool:
        key = (image, manifest_sha256, policy_sha256)
        if self._verified.get(key, 0.0) > time.monotonic():
            return True
        ok = self._verify(image, manifest_sha256, policy_sha256)
        if ok:
            self._verified[key] = time.monotonic() + self._cache_seconds
        else:
            self._verified.pop(key, None)
        return ok

    def _verify(self, image: str, manifest_sha256: str, policy_sha256: str) -> bool:
        if "@sha256:" not in image:
            return False
        if self._cosign_ok("verify", "--key", self._key, "--output", "json", image) is None:
            return False
        if self._profile == "homelab":
            return True
        out = self._cosign_ok(
            "verify-attestation",
            "--key",
            self._key,
            "--type",
            self._type,
            "--output",
            "json",
            image,
        )
        return out is not None and attestation_matches(out, manifest_sha256, policy_sha256)


def attestation_matches(output: str, manifest_sha256: str, policy_sha256: str) -> bool:
    """True when some DSSE envelope in cosign's JSON-lines output carries both digests."""
    for line in output.splitlines():
        try:
            envelope = json.loads(line)
            statement = json.loads(base64.b64decode(envelope["payload"]))
            predicate = statement["predicate"]
        except (ValueError, KeyError, TypeError):
            continue
        if (
            predicate.get("manifestSha256") == manifest_sha256
            and predicate.get("policySha256") == policy_sha256
        ):
            return True
    return False
