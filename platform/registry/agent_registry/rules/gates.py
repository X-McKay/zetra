"""The runtime's verdict on a release that is waiting for approval.

The Registry Controller verifies the signature and the attestations and compiles the policy;
it writes what it found into the AgentRelease's status. These gates read that status from the
last observation. They are the five gates `just agent approve` applies on the pull request
path, unchanged: the registry adds no gate and drops none.

Cases: `contract/gate-cases.json`.
"""

from __future__ import annotations

import re
from typing import Any

from agent_registry.derive import condition
from agent_registry.models import TrustedRepository

ISSUER = "https://token.actions.githubusercontent.com"
ATTESTATIONS = frozenset(
    {
        "https://slsa.dev/provenance/v1",
        "https://spdx.dev/Document",
        "https://cosign.sigstore.dev/attestation/vuln/v1",
        "https://agentplatform.io/eval/v1",
        "https://agentplatform.io/attestations/release/v1",
    }
)
SPEC_KEYS = ("agent", "image", "manifestSha256", "policySha256", "lockSha256", "buildRevision")


def subject_prefix(repository: str, agent: str) -> str:
    """How the signing identity of an agent's own release workflow begins."""
    return f"https://github.com/{repository}/.github/workflows/release.yml@refs/tags/{agent}/"


def _within_pin(pin: TrustedRepository, subject: str) -> bool:
    try:
        return re.fullmatch(pin.subject_reg_exp, subject) is not None
    except re.error:
        return False


def gate_failures(
    release_document: dict[str, Any],
    observed: dict[str, Any],
    pin: TrustedRepository | None,
) -> list[str]:
    """Every reason the observed AgentRelease may not be approved, in a fixed order.

    `release_document` is the registry's record of the release (`Release.document`).
    `observed` is the AgentRelease as last seen on the cluster (`Observation.document`).
    `pin` is the trusted repository that lists the agent for this cluster and tenant.
    An empty list means the release waits on approval and on nothing else.
    """
    failures: list[str] = []
    want = release_document.get("spec") or {}
    spec, status = observed.get("spec") or {}, observed.get("status") or {}
    agent = str(want.get("agent") or "")

    if (observed.get("metadata") or {}).get("name") != (release_document.get("metadata") or {}).get(
        "name"
    ):
        failures.append("live object is not this release")
    for key in SPEC_KEYS:
        if spec.get(key) != want.get(key):
            failures.append(f"live spec.{key} differs from the release")
    if (spec.get("approval") or {}).get("approved") is not False:
        failures.append("live release is not in the unapproved state")

    signature = condition(observed, "SignatureVerified")
    if (
        status.get("signatureVerified") is not True
        or signature.get("status") != "True"
        or signature.get("reason") != "Verified"
    ):
        failures.append("controller has not verified the signature")

    signer = status.get("verifiedSigner") or {}
    subject = str(signer.get("subject") or "")
    if pin is None or agent not in pin.agents:
        failures.append("no trusted repository lists this agent")
    else:
        if signer.get("issuer") != ISSUER or not subject.startswith(
            subject_prefix(pin.repository, agent)
        ):
            failures.append("verified signer is not this agent's release workflow")
        if pin.issuer != ISSUER or not _within_pin(pin, subject):
            failures.append("verified signer subject is outside the tenant signer pin")

    attestations = signer.get("attestations") or []
    if set(attestations) != ATTESTATIONS or len(attestations) != len(ATTESTATIONS):
        failures.append("the five required attestations were not all verified")

    drift = condition(observed, "PolicyDrift")
    if (
        status.get("policyDrift") is not False
        or drift.get("status") != "False"
        or drift.get("reason") != "NoDrift"
    ):
        failures.append("policy digest drift is not clear")
    if status.get("policyDigest") != "sha256:" + str(want.get("policySha256")):
        failures.append("controller policy digest differs from the release's policySha256")

    if status.get("ready") is not False or condition(observed, "Ready").get("reason") != (
        "NotApproved"
    ):
        failures.append("release is not waiting solely on approval")
    return failures
