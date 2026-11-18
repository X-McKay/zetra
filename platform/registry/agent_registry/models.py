"""The records of the registry.

One class per table of `migrations/0001_init.sql`, `migrations/0002_delivery.sql` and
`migrations/0003_alias_moves.sql`. Python names are snake_case; JSON names are camelCase. The same classes are what the store returns and what the HTTP interface carries.

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

# The phases of design section 5.3 (`contract/delivery.md` section 5).
Phase = Literal[
    "awaiting-runtime",  # projected; waiting for the controller's verdict or for Ready
    "pending-approval",  # the controller's gates pass and no approval covers the change
    "activated",  # it has been the current revision
    "superseded",  # replaced by a later revision before it was ever current
    "evaluating",  # approved and Ready; its evaluation has not passed yet
    "refused",  # it failed its evaluation, or its release was revoked before it was current
]

EvaluationState = Literal["pending", "claimed", "passed", "failed", "error", "cancelled"]
RevocationReason = Literal["security", "quality"]
RevocationSessions = Literal["cancel", "finish"]
MoveState = Literal["holding", "held", "moving", "evaluating", "completed", "cancelled"]
HoldState = Literal["holding", "held", "suspended", "resumed", "released"]

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


class Evaluation(Record):
    """One attempt to evaluate one revision (`contract/delivery.md` section 6). `id` to
    `created_at` are written once. The fields after the marker are progress; the store changes
    them only through `Tx.set_evaluation_progress`."""

    id: str
    deployment_id: str
    revision: int
    attempt: int  # 1 for the first
    suite: dict[str, Any] = Field(default_factory=dict)  # the suite as the release carried it
    suite_digest: str = ""  # canonical.digest(suite)
    created_at: datetime
    # progress
    state: EvaluationState = "pending"
    claimed_by: str | None = None
    claim_expires_at: datetime | None = None
    evaluator_version: str = ""
    result: dict[str, Any] = Field(default_factory=dict)  # {verdict, summary, cases: [...]}
    finished_at: datetime | None = None


class Revocation(Record):
    """A release that must not run, indexed from the feed (`contract/delivery.md` section 8).
    Written once. The reviewed file in the cluster repository is the authority."""

    id: str
    kind: Kind
    item: str
    release: str  # the release name
    reason: RevocationReason
    sessions: RevocationSessions  # what happens to sessions already running on it
    summary: str = ""
    revoked_by: str
    revoked_at: datetime
    indexed_at: datetime  # when this registry first saw it; not compared on a second write


class Assessment(Record):
    """What moving an alias to a candidate would touch (`contract/dependencies.md` section 5),
    as it was computed. Written once. `pair` is the `{engine, release}` behind the alias then,
    `candidate` the one asked about, and each dependant is the object of section 5 (its keys
    are camelCase, as they are on the wire)."""

    id: str
    cluster: str
    alias: str
    pair: dict[str, Any]
    candidate: dict[str, Any]
    dependants: list[dict[str, Any]] = Field(default_factory=list)
    blocked: bool
    digest: str  # sha256:<hex>, `derive.sha` of the fields section 5 names
    created_by: str
    created_at: datetime


class AliasMove(Record):
    """One move of an alias (`contract/dependencies.md` section 6). `id` to `started_at` are
    written once. The fields after the marker are progress; the store changes them only through
    `Tx.set_alias_move_progress`."""

    id: str
    assessment_id: str
    cluster: str
    alias: str
    candidate: dict[str, Any]
    sessions: RevocationSessions
    evaluation_seconds: int
    started_by: str
    started_at: datetime
    # progress
    state: MoveState = "holding"
    held_at: datetime | None = None
    moved_at: datetime | None = None
    evaluating_at: datetime | None = None
    deadline: datetime | None = None
    finished_at: datetime | None = None
    reason: str = ""


class Hold(Record):
    """The hold of one agent deployment for one move. `move_id` and `deployment_id` are written
    once; the rest is progress, changed only through `Tx.set_hold_progress`. A hold is in force
    while it is `holding`, `held` or `suspended`."""

    move_id: str
    deployment_id: str
    # progress
    state: HoldState = "holding"
    reason: str = ""
    running: int | None = None  # the last reported number of running task sessions
    acknowledged_at: datetime | None = None
    ended_at: datetime | None = None


IN_FORCE = ("holding", "held", "suspended")
OPEN_MOVE = ("holding", "held", "moving", "evaluating")


def deployment_id(kind: str, cluster: str, tenant: str | None, name: str) -> str:
    return f"{kind}:{cluster}:{tenant or '-'}:{name}"


def release_id(kind: str, item: str, name: str) -> str:
    if not RELEASE_NAME.match(name):
        raise ValueError(f"release name {name!r} is not a release name")
    return f"{kind}:{item}:{name}"


def revision_id(deployment: str, number: int) -> str:
    return f"{deployment}@{number}"


def evaluation_id(deployment: str, number: int, attempt: int) -> str:
    return f"{deployment}@{number}#{attempt}"


def release_slug(text: str) -> str:
    """A release name from free text such as a served model name or an image tag."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return slug[:128] or "unnamed"
