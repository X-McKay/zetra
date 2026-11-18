"""The release report (`contract/delivery.md` section 3): the release workflow tells the
registry what it published. Caller: a GitHub Actions identity token, nothing else.

The report is a claim. It records a release as the next revision, unapproved; the lineage
(repository, tag, commit, workflow) is the token's, never the body's. Checks run in the order
of the table in section 3.2.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from agent_registry.api.auth import AuthError
from agent_registry.api.deps import get_settings, get_store, now
from agent_registry.api.errors import ApiError
from agent_registry.api.github import GithubClaims
from agent_registry.config import Settings
from agent_registry.models import Record, TrustedRepository, deployment_id
from agent_registry.rules import lifecycle, report
from agent_registry.rules import approval as approvals
from agent_registry.store import Store

router = APIRouter(prefix="/v1", tags=["intake"])

TAG = re.compile(r"^refs/tags/([a-z0-9-]+)/v([0-9][A-Za-z0-9.+-]*)\Z")

StoreDep = Annotated[Store, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
Now = Annotated[datetime, Depends(now)]


class ReportLineageIn(Record):
    """Only the checksums are read. Any other key is ignored: the rest is the token's."""

    model_config = {**Record.model_config, "extra": "ignore"}
    checksums: dict[str, str] = Field(default_factory=dict)


class ReportEvaluationIn(Record):
    suite: dict[str, Any]


class ReportReleaseIn(Record):
    agent: dict[str, Any]
    agent_release: dict[str, Any]
    lineage: ReportLineageIn = Field(default_factory=ReportLineageIn)
    evaluation: ReportEvaluationIn | None = None


class ReportReleaseOut(Record):
    deployment: str
    release: str
    revision: int
    created: bool  # False: this release was reported before; nothing was written


def _claims(request: Request, settings: SettingsDep) -> GithubClaims:
    """The caller's token, checked before the body is read. Reports off: not found."""
    if not settings.report_audience:
        raise ApiError("not-found", "release reports are not enabled on this registry")
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError("not-signed-in", "a bearer token is required")
    try:
        return request.app.state.github_verifier.verify(token.strip())
    except AuthError as error:
        raise ApiError("not-signed-in", f"the token was not accepted: {error}") from None


def _trusted(store: Store, claims: GithubClaims, agent: str, namespace: Any) -> TrustedRepository:
    subject = "https://github.com/" + claims.job_workflow_ref
    matches = [
        t
        for t in store.trusted_repositories()
        if t.repository == claims.repository
        and agent in t.agents
        and re.fullmatch(t.subject_reg_exp, subject)
    ]
    if not matches:
        raise ApiError.forbidden(
            f"{claims.repository} is not trusted to release {agent} from this workflow"
        )
    tenants = {t.tenant: store.tenant(t.cluster, t.tenant) for t in matches}
    for trusted in matches:
        tenant = tenants[trusted.tenant]
        if tenant is not None and tenant.namespace == namespace:
            return trusted
    return matches[0]


@router.post("/reports/releases", response_model=ReportReleaseOut)
def report_release(
    body: ReportReleaseIn,
    claims: Annotated[GithubClaims, Depends(_claims)],
    store: StoreDep,
    at: Now,
) -> ReportReleaseOut:
    """Record a release the workflow published. Nothing is approved or made current."""
    found = TAG.match(claims.ref) if claims.ref_type == "tag" else None
    if found is None:
        raise ApiError.forbidden("a release is reported from a tag <agent>/v<version> only")
    agent_name = found.group(1)
    namespace = (body.agent.get("metadata") or {}).get("namespace")
    trusted = _trusted(store, claims, agent_name, namespace)
    names = {
        (body.agent.get("metadata") or {}).get("name"),
        (body.agent_release.get("spec") or {}).get("agent"),
    }
    if names != {agent_name}:
        raise ApiError(
            "invalid-arguments",
            f"the Agent and the release must both name {agent_name}, the agent of the tag",
        )
    tenant = store.tenant(trusted.cluster, trusted.tenant)
    if tenant is None:
        raise ApiError.forbidden(f"tenant {trusted.tenant} is not in the feed")
    if namespace != tenant.namespace:
        raise ApiError("invalid-arguments", f"agent: namespace must be {tenant.namespace}")
    target = deployment_id("agent", trusted.cluster, trusted.tenant, agent_name)
    approvals.owned(store, target)  # in recovery, not found, the pull request path
    # The release is revoked: refused (section 8), added by the revocation package.
    suite = body.evaluation.suite if body.evaluation else None
    if suite is not None:
        report.check_suite(suite)
    report.check_declaration(body.agent, body.agent_release)

    tag = claims.ref.removeprefix("refs/tags/")
    lineage: dict[str, Any] = {
        "repository": claims.repository,
        "tag": tag,
        "commit": claims.sha,
        "workflow": claims.job_workflow_ref,
        "checksums": body.lineage.checksums,
    }
    if suite is not None:
        lineage["evaluation"] = {"suite": suite}
    with store.transaction(f"github:{claims.repository}@{tag}") as tx:
        release_id, number, created = lifecycle.record_release(
            tx, target, body.agent, body.agent_release, lineage, now=at
        )
    release = store.release(release_id)
    return ReportReleaseOut(
        deployment=target, release=release.name, revision=number, created=created
    )
