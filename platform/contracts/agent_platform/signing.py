"""Keyless signer identity and release attestations (private Sigstore profile).

A tenant namespace declares which CI identity may sign its images. Kyverno and the
Registry Controller both read that one declaration, so they cannot disagree about who
is trusted. The predicate checks here are the same conditions the Kyverno attestation
rule applies, and `agentctl validate` runs them on unsigned local attestations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agent_platform.constants import LABEL_PREFIX

ANNOTATION_SIGNER_ISSUER = LABEL_PREFIX + "signer-issuer"
ANNOTATION_SIGNER_SUBJECT_REGEXP = LABEL_PREFIX + "signer-subject-regexp"
ANNOTATION_REQUIRED_ATTESTATIONS = LABEL_PREFIX + "required-attestations"
ANNOTATION_VULN_EXCEPTIONS = LABEL_PREFIX + "vuln-exceptions"

GITHUB_ACTIONS_ISSUER = "https://token.actions.githubusercontent.com"

PREDICATE_SLSA = "https://slsa.dev/provenance/v1"
PREDICATE_SPDX = "https://spdx.dev/Document"
PREDICATE_VULN = "https://cosign.sigstore.dev/attestation/vuln/v1"
PREDICATE_EVAL = "https://agentplatform.io/eval/v1"
PREDICATE_RELEASE = "https://agentplatform.io/attestations/release/v1"
# The release predicate binds manifestSha256 and policySha256 to the signer; nothing
# else recomputes manifestSha256, so it is part of the default set.
DEFAULT_REQUIRED_ATTESTATIONS = (
    PREDICATE_SLSA,
    PREDICATE_SPDX,
    PREDICATE_VULN,
    PREDICATE_EVAL,
    PREDICATE_RELEASE,
)
KNOWN_PREDICATES = DEFAULT_REQUIRED_ATTESTATIONS
# Release tags are `<agent>/v<version>`; the controller requires <agent> == spec.agent.
TAG_SUBJECT = re.compile(r"@refs/tags/([a-z0-9-]+)/v[0-9][A-Za-z0-9.+-]*$")
_AGENT_NAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")

_SHA1 = re.compile(r"^[a-f0-9]{40}$")
_CVE_ID = re.compile(r"^[A-Za-z0-9.:_-]+$")


def subject_regexp_for(
    repository: str, workflow: str = "release.yml", agents: tuple[str, ...] = ()
) -> str:
    """The identity a repo's release workflow gets from Fulcio on an `<agent>/v*` tag.

    With `agents`, only those agents' tags match. Use it whenever one repo releases
    agents for more than one tenant, or a tenant could accept another tenant's image.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be owner/name")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", workflow):
        raise ValueError("workflow must be a file name under .github/workflows")

    bad = [a for a in agents if not _AGENT_NAME.fullmatch(a)]
    if bad:
        raise ValueError(f"bad agent names: {', '.join(bad)}")

    def lit(text: str) -> str:  # only "." needs escaping in these charsets; RE2 and re agree
        return text.replace(".", "\\.")

    tag = "(?:" + "|".join(sorted(set(agents))) + ")" if agents else "[a-z0-9-]+"

    return (
        "^https://github\\.com/"
        + lit(repository)
        + "/\\.github/workflows/"
        + lit(workflow)
        + "@refs/tags/"
        + tag
        + "/v[0-9][A-Za-z0-9.+-]*$"
    )


@dataclass(frozen=True)
class SignerIdentity:
    issuer: str
    subject_regexp: str
    required_attestations: tuple[str, ...] = DEFAULT_REQUIRED_ATTESTATIONS
    vuln_exceptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.issuer.startswith("https://"):
            raise ValueError(f"{ANNOTATION_SIGNER_ISSUER} must be an https URL")
        body = self.subject_regexp[:-1]
        backslashes = len(body) - len(body.rstrip("\\"))
        if (
            not self.subject_regexp.startswith("^")
            or not self.subject_regexp.endswith("$")
            or backslashes % 2 == 1  # `\$` is a literal dollar, not an anchor
        ):
            # An unanchored pattern would accept any repo whose URL contains the text.
            raise ValueError(f"{ANNOTATION_SIGNER_SUBJECT_REGEXP} must be anchored with ^ and $")
        if _top_level_alternation(self.subject_regexp[1:-1]):
            # `^a|b$` is anchored only on one side per branch, and Python's fullmatch
            # and Go's MatchString disagree about it. Group alternatives instead.
            raise ValueError(
                f"{ANNOTATION_SIGNER_SUBJECT_REGEXP} has a top-level '|'; use ^(?:a|b)$"
            )
        try:
            re.compile(self.subject_regexp)
        except re.error as exc:
            raise ValueError(f"{ANNOTATION_SIGNER_SUBJECT_REGEXP} is not a regexp: {exc}") from None
        unknown = [p for p in self.required_attestations if p not in KNOWN_PREDICATES]
        if unknown:
            raise ValueError(f"unknown predicate types: {', '.join(unknown)}")
        bad = [v for v in self.vuln_exceptions if not _CVE_ID.match(v)]
        if bad:
            raise ValueError(f"bad vulnerability ids: {', '.join(bad)}")

    def matches(self, issuer: str, subject: str) -> bool:
        return issuer == self.issuer and re.fullmatch(self.subject_regexp, subject) is not None

    @classmethod
    def from_annotations(cls, annotations: dict[str, str] | None) -> SignerIdentity:
        a = annotations or {}
        issuer = a.get(ANNOTATION_SIGNER_ISSUER)
        subject = a.get(ANNOTATION_SIGNER_SUBJECT_REGEXP)
        if not issuer or not subject:
            raise ValueError(
                f"namespace needs {ANNOTATION_SIGNER_ISSUER} and {ANNOTATION_SIGNER_SUBJECT_REGEXP}"
            )
        required = _split(a.get(ANNOTATION_REQUIRED_ATTESTATIONS)) or DEFAULT_REQUIRED_ATTESTATIONS
        if PREDICATE_RELEASE not in required:
            # The annotation may add requirements, never drop the manifest binding.
            required = (*required, PREDICATE_RELEASE)
        return cls(issuer, subject, required, _split(a.get(ANNOTATION_VULN_EXCEPTIONS)))

    @classmethod
    def from_namespace(cls, namespace: dict[str, Any] | None) -> SignerIdentity:
        if not namespace:
            raise ValueError("namespace not found")
        return cls.from_annotations((namespace.get("metadata") or {}).get("annotations"))

    def annotations(self) -> dict[str, str]:
        out = {
            ANNOTATION_SIGNER_ISSUER: self.issuer,
            ANNOTATION_SIGNER_SUBJECT_REGEXP: self.subject_regexp,
            ANNOTATION_REQUIRED_ATTESTATIONS: ",".join(self.required_attestations),
        }
        if self.vuln_exceptions:
            out[ANNOTATION_VULN_EXCEPTIONS] = ",".join(self.vuln_exceptions)
        return out


def _top_level_alternation(pattern: str) -> bool:
    depth, in_class, i = 0, False, 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            return True
        i += 1
    return False


def tag_agent(subject: str) -> str | None:
    """The `<agent>` of a release-tag certificate subject, or None."""
    m = TAG_SUBJECT.search(subject)
    return m.group(1) if m else None


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(p.strip() for p in (value or "").split(",") if p.strip())


# -- predicate conditions --------------------------------------------------------------


def eval_problems(predicate: Any, commit: str | None = None) -> list[str]:
    """`https://agentplatform.io/eval/v1`: the agent's eval suite passed at the commit.

    {"suite": str, "commit": <40 hex>, "passed": bool, "total": int, "failed": int,
     "results": [{"name": str, "passed": bool}, ...]}   # results optional
    """
    if not isinstance(predicate, dict):
        return ["eval predicate is not an object"]
    problems = []
    if not isinstance(predicate.get("suite"), str) or not predicate["suite"]:
        problems.append("eval.suite missing")
    if not isinstance(predicate.get("commit"), str) or not _SHA1.match(predicate["commit"]):
        problems.append("eval.commit must be a 40-hex git sha")
    elif commit is not None and predicate["commit"] != commit:
        problems.append("eval.commit is not the release commit")
    total, failed = predicate.get("total"), predicate.get("failed")
    if not isinstance(total, int) or isinstance(total, bool) or total < 1:
        problems.append("eval.total must be a positive integer")
    if not isinstance(failed, int) or isinstance(failed, bool) or failed != 0:
        problems.append("eval.failed must be 0")
    if predicate.get("passed") is not True:
        problems.append("eval.passed is not true")
    for r in predicate.get("results") or []:
        if not isinstance(r, dict) or r.get("passed") is not True:
            problems.append(f"eval case failed: {r.get('name') if isinstance(r, dict) else r}")
    return problems


def vuln_findings(predicate: Any) -> list[tuple[str, str]]:
    """(id, severity) pairs from a cosign vuln/v1 predicate carrying grype or trivy JSON."""
    if not isinstance(predicate, dict):
        raise ValueError("vuln predicate is not an object")
    result = (predicate.get("scanner") or {}).get("result")
    if not isinstance(result, dict):
        raise ValueError("vuln predicate has no scanner.result")
    found: list[tuple[str, str]] = []
    for m in result.get("matches") or []:  # grype
        v = m.get("vulnerability") or {}
        found.append((str(v.get("id", "")), str(v.get("severity", "")).lower()))
    for r in result.get("Results") or []:  # trivy
        for v in r.get("Vulnerabilities") or []:
            found.append((str(v.get("VulnerabilityID", "")), str(v.get("Severity", "")).lower()))
    if "matches" not in result and "Results" not in result:
        raise ValueError("vuln scanner.result is neither grype nor trivy output")
    return found


def vuln_problems(predicate: Any, exceptions: tuple[str, ...] = ()) -> list[str]:
    """No critical findings; high findings only when the tenant lists an exception."""
    try:
        findings = vuln_findings(predicate)
    except ValueError as exc:
        return [str(exc)]
    problems = []
    for vid, severity in findings:
        if severity == "critical":
            problems.append(f"critical vulnerability {vid}")
        elif severity == "high" and vid not in exceptions:
            problems.append(f"high vulnerability {vid} without an exception")
    return problems


def release_problems(predicate: Any, manifest_sha256: str, policy_sha256: str) -> list[str]:
    if not isinstance(predicate, dict):
        return ["release predicate is not an object"]
    problems = []
    if predicate.get("manifestSha256") != manifest_sha256:
        problems.append("release.manifestSha256 differs from the AgentRelease")
    if predicate.get("policySha256") != policy_sha256:
        problems.append("release.policySha256 differs from the AgentRelease")
    return problems
