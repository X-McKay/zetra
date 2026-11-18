"""The records of the registry (milestone 1).

One class per table of `migrations/0001_init.sql`. Python names are snake_case; JSON names are
camelCase. The same classes are what the store returns and what the HTTP interface carries.

Identifiers are text and built by the functions at the end of this file. Nothing else builds one.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

Kind = Literal["agent", "tool-server", "skill", "model"]
Authority = Literal["pull-request", "registry"]
Origin = Literal["built", "adopted"]
RevisionOrigin = Literal["imported", "registry", "recovered"]
ApprovalOrigin = Literal["imported", "registry", "recovered"]
ApprovalState = Literal["active", "superseded"]
Mode = Literal["normal", "recovery"]
# `projection` is the provisioner's last result for one object it was asked to write; its name
# is "<kind>/<object name>", for example "agentrelease/db-admin-2026-11-13-33c8acb2".
SubjectKind = Literal["agent", "agentrelease", "tool-server", "model-engine", "projection"]
RequirementType = Literal["tool", "model", "effect", "skill"]
ProvisionType = Literal["tool", "capability"]

# The phases of design section 5.3. Milestone 1 uses four of them; `evaluating` and `refused`
# arrive with the evaluation gate of milestone 2.
Phase = Literal[
    "awaiting-runtime",  # projected; waiting for the controller's verdict or for Ready
    "pending-approval",  # the controller's gates pass and no approval covers the change
    "activated",  # it has been the current revision
    "superseded",  # replaced by a later revision before it was ever current
    "evaluating",
    "refused",
]

NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
RELEASE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class Record(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )


class Meta(Record):
    """The single row of registry state."""

    mode: Mode = "normal"
    mode_reason: str = ""
    mode_changed_at: datetime | None = None


class Cluster(Record):
    name: str
    trust_domain: str
    gateway: str
    # The Flux Kustomizations whose inventory the provisioner reads before it writes.
    flux_inventories: list[dict[str, str]] = Field(default_factory=list)


class Tenant(Record):
    """A tenant on a cluster, with the tenant policy revision last seen in the feed."""

    cluster: str
    name: str
    namespace: str
    policy_revision: str  # sha256 hex of the canonical feed entry
    document: dict[str, Any] = Field(default_factory=dict)


class TrustedRepository(Record):
    repository: str  # owner/name
    cluster: str
    tenant: str
    issuer: str
    subject_reg_exp: str
    agents: list[str] = Field(default_factory=list)


class Item(Record):
    kind: Kind
    name: str
    owner: str = ""
    source: str = ""  # built: repository and path; adopted: upstream project
    description: str = ""
    retired: bool = False


class Release(Record):
    """One release of an item. Written once.

    For an agent in milestone 1 `name` is the AgentRelease name and `document` is the published
    AgentRelease with `spec.approval` removed (approval is not a fact about the release).
    """

    id: str
    kind: Kind
    item: str
    name: str
    version: str | None = None  # the tag's version, where known
    digest: str  # sha256:<hex> of the artifact, or "" for an image pinned by tag only
    origin: Origin
    lineage: dict[str, Any] = Field(default_factory=dict)
    document: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime


class Requirement(Record):
    release_id: str
    type: RequirementType
    target: str  # tool: backend name; model: gateway backend name; effect, skill: its name
    names: list[str] = Field(default_factory=list)  # tool names, or model names requested
    detail: dict[str, Any] = Field(default_factory=dict)


class Provision(Record):
    release_id: str
    type: ProvisionType
    name: str
    detail: dict[str, Any] = Field(default_factory=dict)


class Deployment(Record):
    """A deployment of any kind. `document` is kind-specific.

    agent: {agent: <the Agent document in force>}. On the pull request path it is the Agent
        last seen in git or on the cluster; under the registry it is the Agent published with
        the newest revision. It is what the provisioner is given. Each revision also keeps
        the Agent that was published with it.
    tool-server: {namespace, workload, image, pinnedByDigest, registered, backend}.
    model: {namespace, workload, servedName, servedNameFrom, backend}.
    """

    id: str
    kind: Kind
    item: str
    cluster: str
    tenant: str | None = None
    namespace: str
    authority: Authority = "pull-request"  # agents only; infrastructure kinds stay pull-request
    enabled: bool = True
    pinned_revision: int | None = None  # the pointer stays on this revision while set
    current_revision: int | None = None
    document: dict[str, Any] = Field(default_factory=dict)
    # The event sequence number at which the projected Agent object last changed.
    generation: int = 0


class Revision(Record):
    """A deployment revision. `number` to `created_at` are written once. The fields after the
    marker are progress and may change; the store changes them only through
    `Tx.set_revision_progress`."""

    id: str
    deployment_id: str
    number: int
    release_id: str
    origin: RevisionOrigin
    agent_document: dict[str, Any] = Field(default_factory=dict)  # the Agent published with it
    authority: dict[str, Any] = Field(default_factory=dict)  # canonical.authority_of(agent)
    authority_digest: str = ""
    policy_revision: str = ""
    created_at: datetime
    # progress
    phase: Phase = "awaiting-runtime"
    approval_id: str | None = None
    reason: str = ""
    activated_at: datetime | None = None
    # The event sequence number at which the projected AgentRelease last changed.
    generation: int = 0


class Resolution(Record):
    revision_id: str
    type: RequirementType
    target: str
    resolved_deployment: str | None = None  # None: nothing registered provides it
    resolved_release: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class Alias(Record):
    cluster: str
    tenant: str | None = None
    alias: str
    deployment_id: str  # a model deployment
    backend: str  # the gateway backend that serves it


class Approval(Record):
    """What a person approved, and what it is bound to (design section 10.3)."""

    id: str
    deployment_id: str
    revision: int
    authority_digest: str  # the grant approved
    base_authority_digest: str  # the grant of the current revision when it was approved
    policy_revision: str
    approved_by: str
    approved_at: datetime
    ticket: str | None = None
    origin: ApprovalOrigin
    state: ApprovalState = "active"
    evidence: dict[str, Any] = Field(default_factory=dict)


class Event(Record):
    seq: int
    at: datetime
    actor: str
    type: str
    deployment_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class Observation(Record):
    """The last thing a cluster's provisioner reported about one subject."""

    cluster: str
    subject_kind: SubjectKind
    namespace: str
    name: str
    observed_at: datetime
    present: bool = True  # False: the provisioner looked and it was not there
    in_flux_inventory: bool | None = None  # agents and agentreleases only
    document: dict[str, Any] = Field(default_factory=dict)


def deployment_id(kind: str, cluster: str, tenant: str | None, name: str) -> str:
    return f"{kind}:{cluster}:{tenant or '-'}:{name}"


def release_id(kind: str, item: str, name: str) -> str:
    if not RELEASE_NAME.match(name):
        raise ValueError(f"release name {name!r} is not a release name")
    return f"{kind}:{item}:{name}"


def revision_id(deployment: str, number: int) -> str:
    return f"{deployment}@{number}"


def release_slug(text: str) -> str:
    """A release name from free text such as a served model name or an image tag."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return slug[:128] or "unnamed"
