"""A small registry for tests: the `db-admin` agent of the fixtures, recorded as the importer
records it, with the cluster's side played by hand.

Nothing here uses the importer, the write rules or the projection rules, so that tests of
those can be written against it. `seed` writes what `contract/import.md` says the importer
writes for this one agent; `take` does what `contract/projection.md` section 7 says taking
authority does; `approval_block` is section 3 of the same file.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from agent_registry import canonical, derive
from agent_registry.api.app import create_app
from agent_registry.api.auth import Principal, StaticVerifier
from agent_registry.config import (
    ANNOTATION_PREFIX,
    EVALUATOR,
    OPERATOR,
    PROVISIONER,
    READER,
    Settings,
)
from agent_registry.models import (
    Approval,
    Cluster,
    Deployment,
    Item,
    Observation,
    Revision,
    Tenant,
    TrustedRepository,
    deployment_id,
    revision_id,
)
from agent_registry.store.memory import MemoryStore

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
CLUSTER, TENANT, NAMESPACE, AGENT = "kubani", "tenant-test", "tenant-test", "db-admin"
DEP = deployment_id("agent", CLUSTER, TENANT, AGENT)
REPOSITORY = "X-McKay/zetra-agents"
PERSON = "al@example.com"

TOKENS = {
    "operator": Principal(
        subject="u-1", username="al", roles=frozenset({OPERATOR}), person=True, email=PERSON
    ),
    "reader": Principal(
        subject="u-2",
        username="guest",
        roles=frozenset({READER}),
        person=True,
        email="guest@example.com",
    ),
    # A client's service account that was given the operator role: still not a person.
    "robot": Principal(
        subject="sa-1",
        username="service-account-ci",
        roles=frozenset({OPERATOR}),
        person=False,
    ),
    "provisioner": Principal(
        subject="sa-2",
        username="service-account-registry-provisioner-kubani",
        roles=frozenset({PROVISIONER}),
        person=False,
        cluster=CLUSTER,
    ),
    "evaluator": Principal(
        subject="sa-3",
        username="service-account-registry-evaluator-kubani",
        roles=frozenset({EVALUATOR}),
        person=False,
        azp="registry-evaluator-kubani",
    ),
    "evaluator-elsewhere": Principal(
        subject="sa-4",
        username="service-account-registry-evaluator-other",
        roles=frozenset({EVALUATOR}),
        person=False,
        azp="registry-evaluator-other",
    ),
    "nobody": Principal(subject="u-3", username="nobody", roles=frozenset(), person=True),
}


def fixture_documents(agent: str = AGENT) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The Agent and the AgentReleases of one fixture agent, releases by name."""
    found_agent, releases = {}, []
    for path in sorted((FIXTURES / "manifests" / NAMESPACE / agent).glob("*.yaml")):
        document = yaml.safe_load(path.read_text()) or {}
        if document.get("kind") == "Agent":
            found_agent = document
        elif document.get("kind") == "AgentRelease":
            releases.append(document)
    return found_agent, sorted(releases, key=lambda r: r["metadata"]["name"])


def subject(tag: str, agent: str = AGENT) -> str:
    """The signing identity of an agent's release workflow at a tag."""
    return f"https://github.com/{REPOSITORY}/.github/workflows/release.yml@refs/tags/{agent}/{tag}"


def verdict(
    release_document: dict[str, Any], *, ready: bool = False, tag: str = "v0.1.9"
) -> dict[str, Any]:
    """The status the Registry Controller writes: waiting on approval and nothing else, or
    (ready) approved and Ready."""
    signer = subject(tag, release_document["spec"]["agent"])
    return {
        "conditions": [
            {"type": "PolicyDrift", "status": "False", "reason": "NoDrift", "message": ""},
            {
                "type": "Ready",
                "status": "True" if ready else "False",
                "reason": "Ready" if ready else "NotApproved",
                "message": "",
            },
            {
                "type": "SignatureVerified",
                "status": "True",
                "reason": "Verified",
                "message": signer,
            },
        ],
        "identityMode": "agent",
        "delegationRequired": False,
        "policyDigest": "sha256:" + release_document["spec"]["policySha256"],
        "policyDrift": False,
        "ready": ready,
        "signatureVerified": True,
        "verifiedSigner": {
            "issuer": "https://token.actions.githubusercontent.com",
            "subject": signer,
            "attestations": [
                "https://slsa.dev/provenance/v1",
                "https://spdx.dev/Document",
                "https://cosign.sigstore.dev/attestation/vuln/v1",
                "https://agentplatform.io/eval/v1",
                "https://agentplatform.io/attestations/release/v1",
            ],
        },
    }


SUITE: dict[str, Any] = {
    "schema_version": 1,
    "timeout_seconds": 600,
    "acceptance": {"min_passed": "all"},
    "cases": [
        {"id": "summary", "task": "Report the server version.", "expect": {}},
        {"id": "blocking", "task": "Report any blocking queries.", "expect": {}},
    ],
}


def published(
    letter: str,
    change: Callable[[dict[str, Any]], None] | None = None,
    suite: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The body of `POST .../releases` for a new release of db-admin.

    `letter` is one hex digit; it makes the name and the digests. `change` edits the Agent's
    spec, for a release that asks for something else. `suite` is the evaluation suite it carries.
    """
    agent, _ = fixture_documents()
    agent = canonical.agent_document(agent)
    if change is not None:
        change(agent["spec"])
    body = {
        "agent": agent,
        "agentRelease": {
            "apiVersion": "agentplatform.io/v1alpha1",
            "kind": "AgentRelease",
            "metadata": {"name": f"{AGENT}-2026-11-18-{letter * 8}", "namespace": NAMESPACE},
            "spec": {
                "agent": AGENT,
                "image": f"registry.almckay.io/agents/{AGENT}@sha256:{letter * 64}",
                "manifestSha256": canonical.digest(agent),
                "policySha256": canonical.digest({"policy": letter}),
                "lockSha256": canonical.digest({"lock": letter}),
                "buildRevision": letter * 40,
            },
        },
        "lineage": {"repository": REPOSITORY, "tag": f"{AGENT}/v0.2.{int(letter, 16)}"},
    }
    if suite is not None:
        body["evaluation"] = {"suite": suite}
    return body


def approval_block(store, revision: Revision) -> dict[str, Any]:
    """`spec.approval` of a revision's projected AgentRelease (projection.md section 3)."""
    if revision.approval_id is None:
        return {"approved": False}
    approval = store.approval(revision.approval_id)
    if approval.origin != "registry":
        return copy.deepcopy(approval.evidence["approval"])
    block = {
        "approved": True,
        "approvedBy": approval.approved_by,
        "approvedAt": approval.approved_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if approval.ticket:
        block["ticket"] = approval.ticket
    return block


class World:
    """A store, the application over it, a clock that only moves when told, and a cluster
    that shows what the test says it shows."""

    def __init__(self, settings: Settings | None = None):
        self.now = T0
        self.store = MemoryStore(clock=lambda: self.now)
        self.settings = settings or Settings()
        self.app = create_app(
            self.store, self.settings, StaticVerifier(TOKENS), clock=lambda: self.now
        )
        self.client = TestClient(self.app)

    # -- time and HTTP ---------------------------------------------------------------------

    def tick(self, seconds: float = 1) -> None:
        self.now = self.now + timedelta(seconds=seconds)

    def get(self, path: str, *, token: str | None = "operator", **params: Any):
        return self.client.get(path, params=params or None, headers=_bearer(token))

    def post(self, path: str, body: Any = None, *, token: str | None = "operator"):
        return self.client.post(path, json=body, headers=_bearer(token))

    # -- records ---------------------------------------------------------------------------

    def seed(self) -> None:
        """The cluster, tenant and trusted repository of the feed fixture, and db-admin as the
        importer records it: two approved revisions, the pointer on 2, the pull request path."""
        feed = json.loads((FIXTURES / "feed.json").read_text())
        agent, releases = fixture_documents()
        agent = canonical.agent_document(agent)
        authority = canonical.authority_of(agent)
        with self.store.transaction("test") as tx:
            for entry in feed["clusters"]:
                tx.put_cluster(
                    Cluster(
                        name=entry["name"],
                        trust_domain=entry["trustDomain"],
                        gateway=entry["gateway"],
                        flux_inventories=entry["fluxInventories"],
                    )
                )
            for entry in feed["tenants"]:
                tx.put_tenant(
                    Tenant(
                        cluster=entry["cluster"],
                        name=entry["name"],
                        namespace=entry["namespace"],
                        policy_revision=canonical.digest(entry),
                        document=entry,
                    )
                )
            for entry in feed["trustedRepositories"]:
                tx.put_trusted_repository(
                    TrustedRepository(
                        repository=entry["repository"],
                        cluster=entry["cluster"],
                        tenant=entry["tenant"],
                        issuer=entry["issuer"],
                        subject_reg_exp=entry["subjectRegExp"],
                        agents=entry["agents"],
                    )
                )
            policy_revision = tx.tenant(CLUSTER, TENANT).policy_revision
            tx.put_item(
                Item(
                    kind="agent",
                    name=AGENT,
                    owner=agent["spec"]["owner"],
                    source=REPOSITORY,
                    description=agent["spec"]["description"],
                )
            )
            tx.put_deployment(
                Deployment(
                    id=DEP,
                    kind="agent",
                    item=AGENT,
                    cluster=CLUSTER,
                    tenant=TENANT,
                    namespace=NAMESPACE,
                    document={"agent": agent},
                )
            )
            ordered = sorted(releases, key=lambda r: r["spec"]["approval"]["approvedAt"])
            for number, document in enumerate(ordered, start=1):
                block = document["spec"]["approval"]
                at = datetime.fromisoformat(block["approvedAt"].replace("Z", "+00:00"))
                release = derive.agent_release_record(document, recorded_at=self.now)
                requirements = derive.requirements_of(release.id, agent)
                approval = Approval(
                    id=f"imported:{DEP}@{number}",
                    deployment_id=DEP,
                    revision=number,
                    authority_digest=canonical.authority_digest(authority),
                    base_authority_digest="",
                    policy_revision=policy_revision,
                    approved_by=block["approvedBy"],
                    approved_at=at,
                    origin="imported",
                    evidence={"approval": block},
                )
                tx.add_release(release)
                tx.add_requirements(release.id, requirements)
                tx.add_revision(
                    Revision(
                        id=revision_id(DEP, number),
                        deployment_id=DEP,
                        number=number,
                        release_id=release.id,
                        origin="imported",
                        agent_document=agent,
                        authority=authority,
                        authority_digest=canonical.authority_digest(authority),
                        policy_revision=policy_revision,
                        created_at=self.now,
                        phase="activated",
                        activated_at=at,
                    )
                )
                tx.add_resolutions(
                    revision_id(DEP, number),
                    derive.resolutions_of(tx, CLUSTER, DEP, number, requirements),
                )
                tx.add_approval(approval)
                tx.set_revision_progress(DEP, number, approval_id=approval.id)
            deployment = tx.deployment(DEP)
            tx.put_deployment(deployment.model_copy(update={"current_revision": len(ordered)}))
            tx.event("agent-imported", deployment_id=DEP)

    def take(self) -> None:
        """Authority moves to the registry: every object's generation becomes the event's."""
        with self.store.transaction(PERSON) as tx:
            seq = tx.event("authority-taken", deployment_id=DEP).seq
            deployment = tx.deployment(DEP)
            tx.put_deployment(
                deployment.model_copy(update={"authority": "registry", "generation": seq})
            )
            for revision in tx.revisions(DEP):
                tx.set_revision_progress(DEP, revision.number, generation=seq)

    def change_policy(self) -> str:
        """The tenant's policy changes in the feed. Returns the new policy revision."""
        tenant = self.store.tenant(CLUSTER, TENANT)
        document = copy.deepcopy(tenant.document)
        document["policy"]["vulnExceptions"] = ["CVE-2026-0001"]
        with self.store.transaction("feed") as tx:
            tx.put_tenant(
                tenant.model_copy(
                    update={"document": document, "policy_revision": canonical.digest(document)}
                )
            )
            tx.event("feed-applied", detail={"changed": [f"tenant:{CLUSTER}/{TENANT}"]})
        return canonical.digest(document)

    # -- the cluster -----------------------------------------------------------------------

    def cluster_shows(
        self,
        number: int,
        *,
        ready: bool | None = None,
        status: dict[str, Any] | None = None,
        generation: int | None = None,
        behind: bool = False,
    ) -> Observation:
        """The provisioner reports revision `number`'s AgentRelease.

        By default the cluster has the object as the registry last projected it, and the
        controller's status is: Ready when the projected block is approved, waiting on
        approval otherwise. `behind=True` reports the object as it was one generation ago
        with no approval: the provisioner has not applied the last change yet.
        """
        revision = self.store.revision(DEP, number)
        release = self.store.release(revision.release_id)
        block = {"approved": False} if behind else self.object_block(revision)
        if generation is None:
            generation = revision.generation - 1 if behind else revision.generation
        if ready is None:
            ready = block.get("approved") is True
        observation = Observation(
            cluster=CLUSTER,
            subject_kind="agentrelease",
            namespace=NAMESPACE,
            name=release.name,
            observed_at=self.now,
            in_flux_inventory=False,
            document={
                "metadata": {
                    "name": release.name,
                    "namespace": NAMESPACE,
                    "uid": f"uid-{release.name}",
                    "resourceVersion": str(generation),
                    "annotations": {
                        ANNOTATION_PREFIX + "deployment": DEP,
                        ANNOTATION_PREFIX + "cluster": CLUSTER,
                        ANNOTATION_PREFIX + "revision": str(number),
                        ANNOTATION_PREFIX + "generation": str(generation),
                    },
                },
                "spec": {**copy.deepcopy(release.document["spec"]), "approval": block},
                "status": status if status is not None else verdict(release.document, ready=ready),
            },
        )
        self.store.put_observations(CLUSTER, [observation])
        return observation

    def object_block(self, revision: Revision) -> dict[str, Any]:
        """`spec.approval` of the one object a release has: the newest revision of the release
        that has an approval decides it (delivery.md section 5.1)."""
        for other in reversed(self.store.revisions(DEP)):
            if other.release_id == revision.release_id and other.approval_id is not None:
                return approval_block(self.store, other)
        return {"approved": False}

    def cluster_shows_agent(self) -> Observation:
        """The provisioner reports the Agent as the registry last projected it."""
        deployment = self.store.deployment(DEP)
        annotations = {
            ANNOTATION_PREFIX + "deployment": DEP,
            ANNOTATION_PREFIX + "cluster": CLUSTER,
            ANNOTATION_PREFIX + "generation": str(deployment.generation),
        }
        if deployment.current_revision is not None:
            revision = self.store.revision(DEP, deployment.current_revision)
            annotations[ANNOTATION_PREFIX + "current-release"] = self.store.release(
                revision.release_id
            ).name
        spec = copy.deepcopy(deployment.document["agent"]["spec"])
        spec["enabled"] = deployment.enabled and spec.get("enabled", True)
        observation = Observation(
            cluster=CLUSTER,
            subject_kind="agent",
            namespace=NAMESPACE,
            name=AGENT,
            observed_at=self.now,
            in_flux_inventory=False,
            document={
                "metadata": {
                    "name": AGENT,
                    "namespace": NAMESPACE,
                    "uid": f"uid-{AGENT}",
                    "resourceVersion": str(deployment.generation),
                    "annotations": annotations,
                },
                "spec": spec,
                "status": {},
            },
        )
        self.store.put_observations(CLUSTER, [observation])
        return observation

    def cluster_shows_everything(self) -> None:
        self.cluster_shows_agent()
        for revision in self.store.revisions(DEP):
            self.cluster_shows(revision.number)


def _bearer(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def registry_owned() -> World:
    """db-admin imported, authority taken, and every object observed fresh."""
    world = World()
    world.seed()
    world.take()
    world.cluster_shows_everything()
    return world
