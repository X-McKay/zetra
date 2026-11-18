"""A world for alias moves: six agents on an alias, a feed that moves it, and a cluster whose
side is played by hand (`contract/alias-move-cases.json` describes the actions).

Agents are written to the store as milestone 2 leaves an agent that has been released,
approved and activated, so nothing here uses the rules that alias moves hook into."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent_registry import canonical, derive
from agent_registry.api.app import create_app
from agent_registry.api.auth import StaticVerifier
from agent_registry.importer import apply_feed
from agent_registry.models import (
    Approval,
    Deployment,
    Item,
    Observation,
    Revision,
    deployment_id,
    revision_id,
)
from agent_registry.rules import projection
from agent_registry.store import Store, Unavailable
from tests.world import (
    FIXTURES,
    NAMESPACE,
    PERSON,
    SUITE,
    TENANT,
    TOKENS,
    World,
    fixture_documents,
    verdict,
)

CLUSTER = "kubani"
CASES = json.loads((Path(__file__).parents[1] / "contract" / "alias-move-cases.json").read_text())
BACKENDS = {"fast": "llm-fast", "main": "llm-main"}
REPOSITORY = "X-McKay/zetra-agents"


class Down:
    """A store that refuses every call: the database is not there."""

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise Unavailable("the database is unavailable: test")

        return refuse


def dep_of(agent: str) -> str:
    return deployment_id("agent", CLUSTER, TENANT, agent)


class AliasWorld(World):
    def __init__(self, make_store: Callable[[Callable], Store] | None = None):
        super().__init__()
        if make_store is not None:
            self.store = make_store(lambda: self.now)
            self.app = create_app(
                self.store, self.settings, StaticVerifier(TOKENS), clock=lambda: self.now
            )
            self.client = TestClient(self.app)
        self.setup: dict[str, Any] = {}
        self.reporting: set[str] = set()
        self.engine: dict[str, Any] | None = None
        self.assessment: dict[str, Any] | None = None
        self.move_id: str | None = None

    # -- the feed ----------------------------------------------------------------------------

    def feed_for(self, pair: dict[str, str]) -> dict[str, Any]:
        """The feed in which the alias is behind `pair`: its engine serves that release."""
        f = json.loads((FIXTURES / "feed.json").read_text())
        f["toolServers"] = []
        f["trustedRepositories"] = [
            {
                "repository": REPOSITORY,
                "cluster": CLUSTER,
                "tenant": TENANT,
                "issuer": "https://token.actions.githubusercontent.com",
                "subjectRegExp": "^https://github\\.com/X-McKay/zetra-agents/\\.github/workflows/"
                "release\\.yml@refs/tags/[a-z-]+/v[0-9][A-Za-z0-9.+-]*$",
                "agents": sorted(a["agent"] for a in self.setup["agents"]) + ["newcomer"],
            }
        ]
        releases = self.setup["releases"]
        f["modelReleases"] = [
            {
                "schemaVersion": 1,
                "engine": "fast",
                "name": name,
                "servedName": body["servedName"],
                "weights": {
                    "source": f"hf://{body['servedName']}",
                    "revision": None,
                    "files": None,
                },
                "format": "safetensors",
                "quantisation": None,
                "engineImage": "vllm/vllm-openai:v0.30.0-aarch64",
                "serving": {"chatTemplate": None, "toolCallParser": None, "contextLength": None},
                "offers": body["offers"],
                "evidence": {"benchmark": "a.json", "changeRecord": "b.md"},
            }
            for name, body in releases.items()
        ]
        engines = {e["name"]: e for e in f["modelEngines"]}
        engine = engines[pair["engine"]]
        engine["servedName"] = releases[pair["release"]]["servedName"]
        engine["release"] = pair["release"]
        alias = self.setup["alias"]
        for entry in f["aliases"]:
            if entry["alias"] == alias:
                entry["engine"] = pair["engine"]
                entry["backend"] = BACKENDS[alias]
        return f

    def apply_pair(self, pair: dict[str, str]) -> bool:
        return apply_feed(self.store, self.feed_for(pair), now=self.now)

    # -- the agents --------------------------------------------------------------------------

    def add_agent(self, spec: dict[str, Any]) -> None:
        """An agent released, approved (when the registry owns it) and activated on its alias."""
        name, alias = spec["agent"], spec.get("alias", self.setup["alias"])
        agent, _ = fixture_documents()
        agent = canonical.agent_document(agent)
        agent["metadata"]["name"] = name
        capabilities: dict[str, Any] = {
            "models": [
                {
                    "backend": BACKENDS[alias],
                    "models": ["any"],
                    "budget": {"tokensPerDay": 1000, "tokensPerSession": 100},
                }
            ]
        }
        if spec["tools"]:
            capabilities["mcp"] = [
                {
                    "backend": "postgres-admin",
                    "effect": "remote_read",
                    "tools": ["execute_sql"],
                    "delegation": "inherit",
                }
            ]
        agent["spec"]["capabilities"] = capabilities
        agent = canonical.agent_document(agent)
        agent_release = {
            "apiVersion": "agentplatform.io/v1alpha1",
            "kind": "AgentRelease",
            "metadata": {"name": f"{name}-2026-11-18-aaaaaaaa", "namespace": NAMESPACE},
            "spec": {
                "agent": name,
                "image": f"registry.almckay.io/agents/{name}@sha256:{'a' * 64}",
                "manifestSha256": canonical.digest(agent),
                "policySha256": canonical.digest({"policy": name}),
                "lockSha256": canonical.digest({"lock": name}),
                "buildRevision": "a" * 40,
            },
        }
        lineage = {"evaluation": {"suite": SUITE}} if spec["suite"] else {}
        registry = spec["authority"] == "registry"
        dep = dep_of(name)
        with self.store.transaction(PERSON) as tx:
            release = derive.agent_release_record(
                agent_release, recorded_at=self.now, lineage=lineage
            )
            requirements = derive.requirements_of(release.id, agent)
            authority = canonical.authority_of(agent)
            seq = tx.event("agent-activated-for-test", deployment_id=dep).seq
            tx.put_item(Item(kind="agent", name=name, owner="al@example.com", source=REPOSITORY))
            tx.put_deployment(
                Deployment(
                    id=dep,
                    kind="agent",
                    item=name,
                    cluster=CLUSTER,
                    tenant=TENANT,
                    namespace=NAMESPACE,
                    authority=spec["authority"],
                    enabled=spec["enabled"],
                    current_revision=1,
                    document={"agent": agent},
                    generation=seq,
                )
            )
            tx.add_release(release)
            tx.add_requirements(release.id, requirements)
            tx.add_revision(
                Revision(
                    id=revision_id(dep, 1),
                    deployment_id=dep,
                    number=1,
                    release_id=release.id,
                    origin="registry",
                    agent_document=agent,
                    authority=authority,
                    authority_digest=canonical.authority_digest(authority),
                    policy_revision=tx.tenant(CLUSTER, TENANT).policy_revision,
                    created_at=self.now,
                    phase="activated",
                    activated_at=self.now,
                    generation=seq,
                )
            )
            tx.add_resolutions(
                revision_id(dep, 1), derive.resolutions_of(tx, CLUSTER, dep, 1, requirements)
            )
            if registry:
                approval = Approval(
                    id=f"approval:{dep}",
                    deployment_id=dep,
                    revision=1,
                    authority_digest=canonical.authority_digest(authority),
                    base_authority_digest="",
                    policy_revision=tx.tenant(CLUSTER, TENANT).policy_revision,
                    approved_by=PERSON,
                    approved_at=self.now,
                    origin="registry",
                )
                tx.add_approval(approval)
                tx.set_revision_progress(dep, 1, approval_id=approval.id)

    def build(self, setup: dict[str, Any]) -> None:
        """The feed with the alias behind `setup.pair`, and the setup's agents on the alias."""
        self.setup = copy.deepcopy(setup)
        self.apply_pair(setup["pair"])
        for spec in setup["agents"]:
            self.add_agent(spec)

    # -- the cluster -------------------------------------------------------------------------

    def release_observations(self, agent: str) -> list[dict[str, Any]]:
        """The AgentRelease objects of an agent as the cluster has them: each as the registry
        last projected it, with the controller's verdict on it."""
        deployment = self.store.deployment(dep_of(agent))
        newest: dict[str, Revision] = {}
        for revision in self.store.revisions(deployment.id):
            newest[revision.release_id] = revision
        found = []
        for revision in sorted(newest.values(), key=lambda r: r.number):
            release = self.store.release(revision.release_id)
            block = projection.release_approval_block(self.store, deployment, release.id)
            generation = str(revision.generation)
            found.append(
                {
                    "subjectKind": "agentrelease",
                    "namespace": NAMESPACE,
                    "name": release.name,
                    "inFluxInventory": False,
                    "document": {
                        "metadata": {
                            "name": release.name,
                            "namespace": NAMESPACE,
                            "uid": f"uid-{release.name}",
                            "resourceVersion": generation,
                            "annotations": {
                                "registry.agentplatform.io/deployment": deployment.id,
                                "registry.agentplatform.io/cluster": CLUSTER,
                                "registry.agentplatform.io/revision": str(revision.number),
                                "registry.agentplatform.io/generation": generation,
                            },
                        },
                        "spec": {**copy.deepcopy(release.document["spec"]), "approval": block},
                        "status": verdict(release.document, ready=block.get("approved") is True),
                    },
                }
            )
        return found

    def engine_subject(self) -> list[dict[str, Any]]:
        if self.engine is None:
            return []
        return [
            {
                "subjectKind": "model-engine",
                "namespace": self.store.deployment(
                    deployment_id("model", CLUSTER, None, "fast")
                ).namespace,
                "name": "fast",
                "present": True,
                "document": {
                    "images": ["vllm/vllm-openai:v0.30.0-aarch64"],
                    "replicas": 1,
                    "readyReplicas": self.engine["readyReplicas"],
                    "servedName": self.engine["servedName"],
                },
            }
        ]

    def provisioner_pass(self, holds: list[dict[str, Any]] | None = None):
        """The provisioner reports what the cluster has now."""
        body: dict[str, Any] = {
            "observedAt": self.now.isoformat(),
            "lastGeneration": self.store.generation(),
            "objects": [o for a in sorted(self.reporting) for o in self.release_observations(a)],
            "subjects": self.engine_subject(),
        }
        if holds is not None:
            body["holds"] = holds
        answer = self.post(f"/v1/clusters/{CLUSTER}/observations", body, token="provisioner")
        assert answer.status_code == 200, answer.text
        return answer

    def settle(self) -> None:
        """Passes until one changes nothing: the cluster catches up with what the registry says."""
        for _ in range(8):
            before = self.store.generation()
            self.provisioner_pass()
            if self.store.generation() == before:
                return
        raise AssertionError("the registry never settled")

    def clock(self, seconds: float) -> None:
        self.tick(seconds)
        self.provisioner_pass()

    def put_engine(self, ready: int, served: str) -> None:
        self.engine = {"readyReplicas": ready, "servedName": served}
        deployment = self.store.deployment(deployment_id("model", CLUSTER, None, "fast"))
        self.store.put_observations(
            CLUSTER,
            [
                Observation(
                    cluster=CLUSTER,
                    subject_kind="model-engine",
                    namespace=deployment.namespace,
                    name="fast",
                    observed_at=self.now,
                    document={
                        "images": [],
                        "replicas": ready,
                        "readyReplicas": ready,
                        "servedName": served,
                    },
                )
            ],
        )

    # -- what an observer sees ---------------------------------------------------------------

    def held_agents(self) -> set[str]:
        """The agents whose Agent object, as the registry gives it to the cluster, is held."""
        answer = self.get(f"/v1/clusters/{CLUSTER}/desired", token="provisioner")
        assert answer.status_code == 200, answer.text
        held = set()
        for entry in answer.json()["deployments"]:
            agent = next(o for o in entry["objects"] if o["kind"] == "Agent")
            if "registry.agentplatform.io/held" in agent["document"]["metadata"]["annotations"]:
                held.add(entry["agent"])
        return held

    def newest(self, agent: str) -> Revision:
        return self.store.revisions(dep_of(agent))[-1]

    def later(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)
