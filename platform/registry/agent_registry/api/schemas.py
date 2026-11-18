"""What the HTTP interface carries. The records themselves are in `agent_registry.models`.

JSON names are camelCase. A request with an unknown field is refused.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from agent_registry.derive import Freshness
from agent_registry.models import (
    Approval,
    Deployment,
    Evaluation,
    Event,
    Item,
    Mode,
    Phase,
    Provision,
    Record,
    Release,
    Requirement,
    Resolution,
    Revision,
)

# ---- shared ------------------------------------------------------------------------------


class ErrorDetail(Record):
    code: str
    message: str
    retryable: bool


class ErrorOut(Record):
    error: ErrorDetail


class Observed(Record):
    """How fresh the last observation of a subject is (`derive.freshness`)."""

    state: Freshness
    observed_at: datetime | None = None
    age_seconds: int | None = None


class Health(Record):
    status: Literal["ok"]
    mode: Mode


# ---- read --------------------------------------------------------------------------------


class MetaOut(Record):
    mode: Mode
    mode_reason: str
    mode_changed_at: datetime | None
    generation: int


class ItemsOut(Record):
    items: list[Item]


class ContractStatus(Record):
    """The tool contract recorded for a tool server (`contract/dependencies.md` section 2.3)."""

    state: Literal["recorded", "none"]
    digest: str | None = None
    tools: int | None = None


class ItemOut(Record):
    item: Item
    releases: list[Release]
    deployments: list[Deployment]
    # By deployment id: a tool server's contract, or the release a model engine serves.
    contracts: dict[str, ContractStatus] | None = None
    served_releases: dict[str, str | None] | None = None


class ReleaseOut(Record):
    release: Release
    requirements: list[Requirement]
    provisions: list[Provision]


class DeploymentsOut(Record):
    deployments: list[Deployment]


class DeploymentOut(Record):
    deployment: Deployment
    revisions: list[Revision]
    approvals: list[Approval]


class RevisionOut(Record):
    revision: Revision
    release: Release
    resolutions: list[Resolution]
    approval: Approval | None
    evaluations: list[Evaluation] = Field(default_factory=list)  # oldest first


class Dependant(Record):
    deployment: str  # deployment id
    agent: str
    tenant: str | None
    revision: int  # the revision looked at: the current one, or the newest when none is current
    release: str  # release name
    names: list[str]  # the tools, or the model names, that revision asks for


class DependantsOut(Record):
    cluster: str
    type: Literal["tool", "model"]
    name: str  # as asked: a tool backend name, or a model alias
    target: str | None  # the gateway backend looked up; None when the alias is unknown
    provided_by: str | None  # deployment id of the tool server or model; None when unregistered
    dependants: list[Dependant]


class CurrentStatus(Record):
    release: str
    revision: int
    since: datetime | None
    admitted_by: str | None  # the evaluation id, or null when no evaluation admitted it


class CandidateEvaluation(Record):
    attempt: int
    state: str


class CandidateStatus(Record):
    release: str
    revision: int
    phase: str
    reason: str
    evaluation: CandidateEvaluation | None


class RefusedEvaluation(Record):
    verdict: str
    summary: str


class RefusedStatus(Record):
    release: str
    revision: int
    reason: str
    at: datetime | None
    evaluation: RefusedEvaluation | None


class RevokedStatus(Record):
    id: str
    release: str
    reason: str
    at: datetime


class AgentStatus(Record):
    deployment: str
    agent: str
    tenant: str | None
    namespace: str
    authority: Literal["pull-request", "registry"]
    enabled: bool
    pinned_revision: int | None
    current_revision: int | None
    current_release: str | None  # release name
    newest_revision: int | None
    newest_release: str | None
    phase: str | None  # of the newest revision
    ready: bool | None  # the current release's Ready condition; None when not known
    blockers: list[str]  # why it is not running what was last published, in plain words
    next_actions: list[str]  # commands a person can run, in order
    observation: Observed  # of the Agent object
    current: CurrentStatus | None = None
    candidate: CandidateStatus | None = None
    last_refused: RefusedStatus | None = None  # the newest refused revision, if newer than current
    acknowledged: bool | None = None  # the cluster has the Agent at the registry's generation
    pinned: int | None = None
    revoked: list[RevokedStatus] = Field(default_factory=list)  # newest first


class ToolServerStatus(Record):
    deployment: str
    name: str
    namespace: str
    registered: bool
    backend: str | None
    declared_image: str
    pinned_by_digest: bool
    observed_images: list[str]  # empty when the observation is not fresh
    ready_replicas: int | None
    observation: Observed
    dependants: list[str]  # agent names
    contract: ContractStatus


class ModelStatus(Record):
    deployment: str
    name: str
    namespace: str
    aliases: list[str]
    backend: str | None
    declared_served_name: str
    observed_served_name: str | None
    observed_image: str | None
    observation: Observed
    dependants: list[str]
    release: str | None  # the release the document names; None when the feed names none


class StatusOut(Record):
    cluster: str
    mode: Mode
    generation: int
    observation_max_age_seconds: int
    agents: list[AgentStatus]
    tool_servers: list[ToolServerStatus]
    models: list[ModelStatus]


class EventsOut(Record):
    events: list[Event]
    generation: int


# ---- write -------------------------------------------------------------------------------


class SuiteIn(Record):
    """The evaluation suite the release carries (`contract/delivery.md` section 1.3)."""

    suite: dict[str, Any]


class RecordReleaseIn(Record):
    """A published release: the two documents the release workflow attached to the tag."""

    agent: dict[str, Any]
    agent_release: dict[str, Any]
    # Where it came from, for the record: repository, tag, asset checksums. Not interpreted.
    lineage: dict[str, Any] = Field(default_factory=dict)
    evaluation: SuiteIn | None = None


class RecordReleaseOut(Record):
    release: Release
    revision: Revision
    created: bool  # False: this release was recorded before with the same content


class ApprovalPreviewOut(Record):
    """Everything an approval is bound to, for a person to read before approving. The four
    binding fields are sent back unchanged in `ApproveIn`."""

    deployment: str
    revision: int
    release: str
    authority_digest: str
    base_authority_digest: str
    policy_revision: str
    authority: dict[str, Any]  # what the revision asks for
    base_authority: dict[str, Any]  # what the current revision has ({} when there is none)
    changes: list[str]  # every way the authority is more than the base; [] when it is not
    gate_failures: list[str]  # [] when the runtime's verdict allows approval
    observation: Observed  # of the AgentRelease object
    approved: bool  # an approval already covers this revision
    approvable: bool  # no gate failure, a fresh observation, and not yet approved


class ApproveIn(Record):
    approval_id: str = Field(min_length=8, max_length=128)  # made by the client, used once
    revision: int
    authority_digest: str
    base_authority_digest: str
    policy_revision: str
    ticket: str | None = None


class ApproveOut(Record):
    approval: Approval
    revision: Revision


class EnabledIn(Record):
    enabled: bool
    reason: str = ""


class PinIn(Record):
    revision: int | None  # None removes the pin


class DeploymentChangeOut(Record):
    deployment: Deployment
    changed: bool  # False: it was already so, and nothing was written


class RollbackIn(Record):
    revision: int | None = None  # None: the newest earlier activated revision
    reason: str = ""


class EvaluationRequestIn(Record):
    reason: str = ""


class EvaluationRequestOut(Record):
    evaluation: Evaluation
    revision: Revision


class ReturnOut(Record):
    """The documents to write back to git when authority returns to the pull request path."""

    deployment: Deployment
    changed: bool
    agent: dict[str, Any] | None  # the Agent as last projected, without registry annotations
    agent_releases: list[dict[str, Any]]  # every release, with its approval block


class Difference(Record):
    deployment: str
    subject: str  # "agent/<name>" or "agentrelease/<name>"
    field: str  # agent, enabled, approval, release, current-release, generation
    registry: Any
    cluster: Any


class RecoveryOut(Record):
    mode: Mode
    reason: str
    # True when every cluster that has registry-owned agents has reported fresh observations
    # since recovery began; leaving is refused until then.
    complete: bool
    waiting_for: list[str]  # cluster names not yet heard from
    differences: list[Difference]
    digest: str  # of the differences; named when leaving, so that what was read is what is adopted


class LeaveRecoveryIn(Record):
    digest: str


class LeaveRecoveryOut(Record):
    mode: Mode
    generation: int
    adopted: list[Difference]


# ---- evaluations -------------------------------------------------------------------------


class ClaimIn(Record):
    evaluator: str = Field(min_length=1, max_length=128)
    lease_seconds: int = Field(default=900, ge=60, le=3600)


class ClaimedEvaluation(Record):
    deployment: str
    revision: int
    attempt: int
    agent: str
    tenant: str
    namespace: str
    release: str  # the release name
    suite: dict[str, Any]
    suite_digest: str
    claim_expires_at: datetime


class ClaimOut(Record):
    evaluation: ClaimedEvaluation | None


class CaseResultIn(Record):
    id: str
    passed: bool
    session_id: str = ""
    outcome: str = ""
    detail: str = ""


class ResultIn(Record):
    evaluator: str = Field(min_length=1, max_length=128)
    evaluator_version: str = ""
    verdict: Literal["passed", "failed", "error"]
    summary: str = ""
    cases: list[CaseResultIn] = Field(default_factory=list)


class ResultOut(Record):
    recorded: bool  # False: nothing was written
    phase: Phase  # of the revision now


# ---- cluster -----------------------------------------------------------------------------


class ObjectRef(Record):
    api_version: str
    kind: str
    namespace: str
    name: str


class ProjectedObject(Record):
    """One object the provisioner applies. `document` is complete: apiVersion, kind,
    metadata (name, namespace, annotations) and spec. `generation` is repeated from the
    annotation so the provisioner need not parse it."""

    kind: Literal["Agent", "AgentRelease"]
    namespace: str
    name: str
    generation: int
    document: dict[str, Any]


class DesiredDeployment(Record):
    deployment: str
    agent: str
    tenant: str
    namespace: str
    objects: list[ProjectedObject]


class ObserveSubject(Record):
    """A workload the provisioner reports on and never writes."""

    subject_kind: Literal["tool-server", "model-engine"]
    name: str
    namespace: str
    workload: ObjectRef
    served_name_from: dict[str, str] | None = None  # {kind: ConfigMap, name, key}


class DesiredRevocation(Record):
    """A revoked release whose running sessions the provisioner cancels, after it has written
    the release's AgentRelease (`contract/delivery.md` section 8.3)."""

    id: str
    tenant: str
    namespace: str
    agent: str
    release: str
    sessions: Literal["cancel"]


class DesiredOut(Record):
    cluster: str
    mode: Mode
    generation: int
    field_manager: str
    namespaces: list[str]  # tenant namespaces: report every Agent and AgentRelease in them
    flux_inventories: list[dict[str, str]]
    deployments: list[DesiredDeployment]  # registry-owned agents only; empty in recovery
    observe: list[ObserveSubject]
    revocations: list[DesiredRevocation] = Field(default_factory=list)  # empty in recovery


class ObservedObject(Record):
    subject_kind: Literal["agent", "agentrelease"]
    namespace: str
    name: str
    in_flux_inventory: bool | None  # null: the inventory could not be read this pass
    # metadata (name, namespace, uid, resourceVersion, annotations under the registry prefix
    # only), spec and status. Nothing else is sent.
    document: dict[str, Any]


class ObservedSubject(Record):
    subject_kind: Literal["tool-server", "model-engine"]
    namespace: str
    name: str
    present: bool = True
    # tool-server: {images: [...], replicas, readyReplicas}
    # model-engine: {images: [...], replicas, readyReplicas, servedName}
    document: dict[str, Any] = Field(default_factory=dict)


class ProjectionResult(Record):
    deployment: str
    kind: Literal["Agent", "AgentRelease"]
    namespace: str
    name: str
    generation: int
    result: Literal["applied", "unchanged", "refused-flux-inventory", "refused-older", "error"]
    message: str = ""


class SnapshotScope(Record):
    subject_kind: Literal["agent", "agentrelease"]
    namespace: str


class ObservationsIn(Record):
    observed_at: datetime
    last_generation: int  # the highest generation this provisioner ever received
    # The (kind, namespace) pairs for which `objects` lists everything on the cluster.
    complete: list[SnapshotScope] = Field(default_factory=list)
    objects: list[ObservedObject] = Field(default_factory=list)
    subjects: list[ObservedSubject] = Field(default_factory=list)
    projections: list[ProjectionResult] = Field(default_factory=list)


class ObservationsOut(Record):
    mode: Mode
    generation: int
    stored: int


# ---- assessments and alias moves --------------------------------------------------------


class Pair(Record):
    """The engine behind an alias and the release that engine serves."""

    engine: str
    release: str


class AssessmentIn(Record):
    cluster: str
    alias: str
    candidate: Pair


class Need(Record):
    need: str  # toolCalling
    met: bool | None  # the candidate manifest's offer; None when it does not say


class AssessedDependant(Record):
    deployment: str
    agent: str
    tenant: str | None
    authority: Literal["pull-request", "registry"]
    enabled: bool
    release: str | None  # the release of the current revision
    held: bool
    evaluation: Literal["required", "none"]
    needs: list[Need]


class AssessmentOut(Record):
    id: str
    cluster: str
    alias: str
    from_: Pair = Field(alias="from")
    candidate: Pair
    dependants: list[AssessedDependant]
    blocked: bool
    digest: str
    created_by: str
    created_at: datetime
    stale: bool  # computed at the time of the call


class AliasMoveIn(Record):
    assessment: str  # the assessment's id
    sessions: Literal["finish", "cancel"] = "finish"
    # None: REGISTRY_ALIAS_MOVE_EVALUATION_SECONDS
    evaluation_seconds: int | None = Field(default=None, ge=60, le=86400)


class AliasMoveCancelIn(Record):
    reason: str = ""


class HoldOut(Record):
    deployment: str
    agent: str
    state: Literal["holding", "held", "suspended", "resumed", "released"]
    reason: str
    running: int | None
    acknowledged_at: datetime | None
    ended_at: datetime | None


class AliasMoveOut(Record):
    id: str
    assessment_id: str
    cluster: str
    alias: str
    candidate: Pair
    state: Literal["holding", "held", "moving", "evaluating", "completed", "cancelled"]
    sessions: Literal["finish", "cancel"]
    evaluation_seconds: int
    started_by: str
    started_at: datetime
    held_at: datetime | None
    moved_at: datetime | None
    evaluating_at: datetime | None
    deadline: datetime | None
    finished_at: datetime | None
    reason: str
    holds: list[HoldOut]
    next: str  # one sentence: what the move waits for


class AliasMovesOut(Record):
    moves: list[AliasMoveOut]  # newest first


class AliasCheckIn(Record):
    cluster: str
    alias: str
    candidate: Pair


class AliasCheckOut(Record):
    allowed: bool
    move: str | None
    assessment: str | None
    reason: str
