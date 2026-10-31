"""Release signature verification (6.4 AgentRelease step 1). Fails closed.

Two verifiers share one port:

- `CosignVerifier`: `cosign verify --key` against an organisation key (the original
  homelab and enterprise profiles).
- `KeylessVerifier`: keyless certificates from the private Sigstore. The signer must
  match the identity the tenant namespace declares, and every attestation the namespace
  requires must be present, signed by that same identity, and pass its conditions.

Any error, timeout or unexpected output is a failed verification.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Protocol

from agent_platform.signing import (
    PREDICATE_EVAL,
    PREDICATE_RELEASE,
    PREDICATE_VULN,
    SignerIdentity,
    eval_problems,
    release_problems,
    vuln_problems,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Verification:
    ok: bool
    reason: str = ""
    issuer: str = ""
    subject: str = ""
    attestations: tuple[str, ...] = field(default=())

    def __bool__(self) -> bool:
        return self.ok


class SignatureVerifier(Protocol):
    def verify(
        self,
        image: str,
        manifest_sha256: str,
        policy_sha256: str,
        signer: SignerIdentity | None = None,
    ) -> Verification: ...


class _Cosign:
    def __init__(self, cosign: str, timeout: float, run, env: dict[str, str] | None) -> None:
        self._cosign = cosign
        self._timeout = timeout
        self._run = run
        self._env = env

    def __call__(self, *args: str) -> str | None:
        try:
            proc = self._run(
                [self._cosign, *args],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                **({"env": self._env} if self._env is not None else {}),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("cosign could not run: %s", type(exc).__name__)
            return None
        if proc.returncode != 0:
            log.info("cosign %s refused the image (exit %d)", args[0], proc.returncode)
            return None
        return proc.stdout


class _Cache:
    """Positive results only, keyed on everything that decided them."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._hits: dict[tuple, tuple[float, Verification]] = {}

    def get(self, key: tuple) -> Verification | None:
        hit = self._hits.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        return None

    def put(self, key: tuple, result: Verification) -> Verification:
        if result.ok:
            self._hits[key] = (time.monotonic() + self._seconds, result)
        else:
            self._hits.pop(key, None)
        return result


class CosignVerifier:
    """`cosign verify` against the organisation key.

    homelab profile: the image signature only; the policy digest is recomputed by the
    controller itself (step 2). enterprise profile: additionally `cosign verify-attestation`,
    whose predicate must carry `manifestSha256` and `policySha256` equal to the spec.
    """

    def __init__(
        self,
        key_path: str,
        profile: str = "homelab",
        predicate_type: str = PREDICATE_RELEASE,
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
        self._cosign = _Cosign(cosign, timeout, run, None)
        self._cache = _Cache(cache_seconds)

    def verify(self, image, manifest_sha256, policy_sha256, signer=None) -> Verification:
        key = (image, manifest_sha256, policy_sha256)
        return self._cache.get(key) or self._cache.put(
            key, self._verify(image, manifest_sha256, policy_sha256)
        )

    def _verify(self, image: str, manifest_sha256: str, policy_sha256: str) -> Verification:
        if "@sha256:" not in image:
            return Verification(False, "ImageNotPinned")
        if self._cosign("verify", "--key", self._key, "--output", "json", image) is None:
            return Verification(False, "SignatureRejected")
        if self._profile == "homelab":
            return Verification(True, "Verified")
        out = self._cosign(
            "verify-attestation",
            "--key",
            self._key,
            "--type",
            self._type,
            "--output",
            "json",
            image,
        )
        if out is not None and attestation_matches(out, manifest_sha256, policy_sha256):
            return Verification(True, "Verified", attestations=(self._type,))
        return Verification(False, "AttestationRejected")


class KeylessVerifier:
    """Keyless verification against the private Sigstore trust root.

    `trusted_root` is a Sigstore trusted_root.json (Fulcio CA, Rekor and CT log keys),
    mounted from the private TUF repo's targets. cosign checks the certificate chain,
    the SCT and the Rekor inclusion proof; this class then checks the identity cosign
    reports against the namespace's declaration rather than trusting the flags alone.
    """

    def __init__(
        self,
        trusted_root: str,
        rekor_url: str = "",
        cosign: str = "cosign",
        timeout: float = 60.0,
        cache_seconds: float = 600.0,
        run=subprocess.run,
        env: dict[str, str] | None = None,
    ) -> None:
        if not trusted_root:
            raise ValueError("keyless verification needs a trusted root")
        self._trust = ["--trusted-root", trusted_root]
        if rekor_url:
            self._trust += ["--rekor-url", rekor_url]
        self._cosign = _Cosign(cosign, timeout, run, env)
        self._cache = _Cache(cache_seconds)

    def verify(self, image, manifest_sha256, policy_sha256, signer=None) -> Verification:
        if signer is None:
            return Verification(False, "SignerIdentityMissing")
        key = (image, manifest_sha256, policy_sha256, signer)
        return self._cache.get(key) or self._cache.put(
            key, self._verify(image, manifest_sha256, policy_sha256, signer)
        )

    def _identity(self, signer: SignerIdentity) -> list[str]:
        return [
            *self._trust,
            "--certificate-oidc-issuer",
            signer.issuer,
            "--certificate-identity-regexp",
            signer.subject_regexp,
            "--output",
            "json",
        ]

    def _verify(
        self, image: str, manifest_sha256: str, policy_sha256: str, signer: SignerIdentity
    ) -> Verification:
        if "@sha256:" not in image:
            return Verification(False, "ImageNotPinned")
        out = self._cosign("verify", *self._identity(signer), image)
        if out is None:
            return Verification(False, "SignatureRejected")
        identity = signing_identity(out)
        if identity is None or not signer.matches(*identity):
            return Verification(False, "SignerMismatch")

        for predicate_type in signer.required_attestations:
            out = self._cosign(
                "verify-attestation", *self._identity(signer), "--type", predicate_type, image
            )
            if out is None:
                return Verification(False, "AttestationMissing", *identity)
            # Attestations accumulate on a digest. Every one of this type must pass,
            # so a later failing scan or eval cannot be masked by an earlier clean one
            # (Kyverno evaluates attestation conditions the same way).
            predicates = list(predicates_of(out, predicate_type))
            if not predicates:
                log.info("no %s predicate for %s", predicate_type, image)
                return Verification(False, "AttestationMissing", *identity)
            for predicate in predicates:
                why = _problems(predicate_type, predicate, manifest_sha256, policy_sha256, signer)
                if why:
                    log.info("attestation %s refused for %s: %s", predicate_type, image, why)
                    return Verification(False, "AttestationRejected", *identity)
        return Verification(True, "Verified", *identity, signer.required_attestations)


def _problems(predicate_type, predicate, manifest_sha256, policy_sha256, signer) -> list[str]:
    if predicate is None:
        return ["no predicate of this type"]
    if predicate_type == PREDICATE_EVAL:
        return eval_problems(predicate)
    if predicate_type == PREDICATE_VULN:
        return vuln_problems(predicate, signer.vuln_exceptions)
    if predicate_type == PREDICATE_RELEASE:
        return release_problems(predicate, manifest_sha256, policy_sha256)
    return []  # provenance and SBOM: presence under the pinned identity is the gate


def signing_identity(output: str) -> tuple[str, str] | None:
    """(issuer, subject) from `cosign verify --output json`, when every signature agrees."""
    try:
        sigs = json.loads(output)
    except ValueError:
        return None
    found = set()
    for sig in sigs if isinstance(sigs, list) else []:
        optional = (sig or {}).get("optional") or {}
        issuer, subject = optional.get("Issuer"), optional.get("Subject")
        if not isinstance(issuer, str) or not isinstance(subject, str):
            return None
        found.add((issuer, subject))
    return found.pop() if len(found) == 1 else None


def _statements(output: str):
    for line in output.splitlines():
        try:
            envelope = json.loads(line)
            yield json.loads(base64.b64decode(envelope["payload"]))
        except (ValueError, KeyError, TypeError):
            continue


def predicates_of(output: str, predicate_type: str):
    for statement in _statements(output):
        if isinstance(statement, dict) and statement.get("predicateType") == predicate_type:
            yield statement.get("predicate")


def attestation_matches(output: str, manifest_sha256: str, policy_sha256: str) -> bool:
    """True when some DSSE envelope in cosign's JSON-lines output carries both digests."""
    for statement in _statements(output):
        try:
            predicate = statement["predicate"]
        except (KeyError, TypeError):
            continue
        if isinstance(predicate, dict) and not release_problems(
            predicate, manifest_sha256, policy_sha256
        ):
            return True
    return False
