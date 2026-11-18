"""The three programs of the platform around one fake cluster, with one clock.

Real: the registry's application (called over HTTP in process), its store (memory or
PostgreSQL), the provisioner's pass with its own HTTP client, and the Registry Controller's
reconciler. Played: the Kubernetes API server (`loopworld.cluster`), Flux, Keycloak, and the
signature check. Tokens are names: the registry is given a table from name to caller.
"""

from __future__ import annotations

import copy
import json
import types
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from agent_platform.bundle import compile_bundle, digest_hex
from agent_platform.constants import PlatformConfig
from agent_platform.signing import (
    ANNOTATION_SIGNER_ISSUER,
    ANNOTATION_SIGNER_SUBJECT_REGEXP,
    DEFAULT_REQUIRED_ATTESTATIONS,
    GITHUB_ACTIONS_ISSUER,
)
from agent_registry import canonical, importer
from agent_registry.api.app import create_app
from agent_registry.api.auth import Principal, StaticVerifier
from agent_registry.api.github import GithubClaims, StaticGithubVerifier
from agent_registry.config import (
    EVALUATOR,
    FIELD_MANAGER,
    OPERATOR,
    PROVISIONER,
    READER,
    Settings,
)
from agent_registry.store import Store
from loopworld.cluster import AV, Cluster
from loopworld.sessions import PlayedTemporal, session_app
from registry_controller.config import ControllerConfig
from registry_controller.reconciler import Reconciler
from registry_controller.signature import Verification
from registry_evaluator.cycle import Worker
from registry_evaluator.evaluate import Runtime
from registry_evaluator.registry import HttpRegistry as EvaluatorRegistry
from registry_evaluator.sessions import HttpSessions as EvaluatorSessions
from registry_provisioner.cycle import run_pass
from registry_provisioner.registry import HttpRegistry
from registry_provisioner.sessions import HttpSessions as ProvisionerSessions

FIXTURES = Path(__file__).resolve().parents[2] / "registry" / "tests" / "fixtures"
T0 = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
CLUSTER, TENANT = "kubani", "tenant-test"
REPOSITORY = "X-McKay/zetra-agents"
PERSON = "al@example.com"
PLATFORM_NAMESPACE = "agent-platform"

# What Kubani gives its Registry Controller. The committed releases were compiled for these.
MCP_RESOURCE_BASE = "https://mcp.almckay.io"
TRUST_DOMAIN = "cluster.local"

TOKENS = {
    "operator": Principal(
        subject="u-1", username="al", roles=frozenset({OPERATOR}), person=True, email=PERSON
    ),
    "reader": Principal(
        subject="sa-1",
        username="service-account-registry-reader",
        roles=frozenset({READER}),
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
        azp=f"registry-evaluator-{CLUSTER}",
    ),
}
REPORT_AUDIENCE = "agent-registry-reports"


def feed() -> dict[str, Any]:
    return json.loads((FIXTURES / "feed.json").read_text())


def committed(agent: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The Agent and the AgentReleases of one agent as git holds them, releases by name."""
    found, releases = {}, []
    for path in sorted((FIXTURES / "manifests" / TENANT / agent).glob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text()):
            if (document or {}).get("kind") == "Agent":
                found = document
            elif (document or {}).get("kind") == "AgentRelease":
                releases.append(document)
    return found, sorted(releases, key=lambda r: r["metadata"]["name"])


def agents() -> list[str]:
    return sorted(p.name for p in (FIXTURES / "manifests" / TENANT).iterdir() if p.is_dir())


def signer_subject(agent: str, tag: str) -> str:
    return f"https://github.com/{REPOSITORY}/.github/workflows/release.yml@refs/tags/{agent}/{tag}"


class _Token:
    """A caller's token source: the name the registry knows the caller by."""

    def __init__(self, name: str = "provisioner") -> None:
        self.name = name

    def token(self) -> str:
        return self.name

    def invalidate(self) -> None:
        pass


class _Idp:
    def ensure_agent(self, spec) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, spec.client_id))

    def delete_agent(self, client_id, claims_scope) -> None:
        pass


class Signatures:
    """The signature check, played. An image is signed once the test says which release tag
    it was built from; the answer then carries that workflow's identity and the five
    attestations, and holds only if the tenant's pin accepts the identity."""

    def __init__(self) -> None:
        self.tags: dict[str, tuple[str, str]] = {}  # image -> (agent, tag)

    def sign(self, image: str, agent: str, tag: str) -> None:
        self.tags[image] = (agent, tag)

    def verify(self, image, manifest_sha256, policy_sha256, signer=None) -> Verification:
        if image not in self.tags or signer is None:
            return Verification(False, "NoSignature")
        subject = signer_subject(*self.tags[image])
        if not signer.matches(GITHUB_ACTIONS_ISSUER, subject):
            return Verification(False, "SignerMismatch", GITHUB_ACTIONS_ISSUER, subject)
        return Verification(
            True, "Verified", GITHUB_ACTIONS_ISSUER, subject, tuple(DEFAULT_REQUIRED_ATTESTATIONS)
        )


class World:
    def __init__(self, make_store: Callable[[Callable[[], datetime]], Store]) -> None:
        self.now = T0
        self.cluster = Cluster()
        self.store = make_store(lambda: self.now)
        self.settings = Settings(report_audience=REPORT_AUDIENCE)
        self.github = StaticGithubVerifier({})
        self.app = create_app(
            self.store,
            self.settings,
            StaticVerifier(TOKENS),
            github_verifier=self.github,
            clock=lambda: self.now,
        )
        self.http = TestClient(self.app)
        self.registry = HttpRegistry("http://testserver", CLUSTER, _Token(), self.http)
        self.signatures = Signatures()
        # The tenant's session interface, and the Temporal behind it.
        self._sessions()
        self.feed_document = feed()
        self.controller = self._controller()
        self._platform_objects()

    def _sessions(self) -> None:
        self.temporal = PlayedTemporal(
            now=lambda: self.now,
            approval_of=lambda name: ((self.release(name) or {}).get("spec") or {}).get("approval"),
        )
        self.sessions_http = TestClient(
            session_app(TENANT, CLUSTER, self.cluster, self.temporal, lambda: self.now)
        )
        self.workers: dict[str, Worker] = {}

    def _controller(self) -> Reconciler:
        return Reconciler(
            self.cluster,
            _Idp(),
            self.signatures,
            ControllerConfig(
                platform=PlatformConfig(
                    issuer_url=self.settings.issuer,
                    mcp_resource_base=MCP_RESOURCE_BASE,
                    gateway_cluster_ip="10.43.250.10",
                    egress_guard_image="registry.almckay.io/platform/egress-guard@sha256:"
                    + "5b" * 32,
                    trust_domain=TRUST_DOMAIN,
                ),
                idp_alias=f"kubernetes-{CLUSTER}",
                signature_profile="keyless",
            ),
            now=lambda: self.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def serve(self, other: World) -> None:
        """Become the registry of another world's cluster, at that world's time: what a
        registry restored from an earlier dump is to the cluster that kept running."""
        self.cluster, self.signatures, self.now = other.cluster, other.signatures, other.now
        self.controller = self._controller()
        self._sessions()

    # -- the cluster as the platform's own manifests leave it ------------------------------

    def _platform_objects(self) -> None:
        pin = next(t for t in feed()["tenants"] if t["name"] == TENANT)["policy"]["signer"]
        for name, pinned in (
            (
                TENANT,
                {
                    ANNOTATION_SIGNER_ISSUER: pin["issuer"],
                    ANNOTATION_SIGNER_SUBJECT_REGEXP: pin["subjectRegExp"],
                },
            ),
            (PLATFORM_NAMESPACE, {}),
            ("mcp", {}),
        ):
            self.cluster.put(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": name, "namespace": None, "annotations": pinned},
                }
            )
        self.cluster.put(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "agent-gateway-ca", "namespace": PLATFORM_NAMESPACE},
                "data": {
                    "ca.crt": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
                },
            }
        )

    def flux_applies(self, agent: str) -> list[dict[str, Any]]:
        """Flux reconciles one agent's directory: its Agent and AgentReleases, as committed.
        Each committed release was signed by its own release tag."""
        declared, releases = committed(agent)
        for number, release in enumerate(releases, start=1):
            self.signatures.sign(release["spec"]["image"], agent, f"v0.1.{number}")
        for document in (declared, *releases):
            self.cluster.flux_apply(document)
        return [declared, *releases]

    def workload(self, namespace: str, name: str, image: str, *, ready: int = 1) -> None:
        self.cluster.put(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"namespace": namespace, "name": name},
                "spec": {
                    "replicas": 1,
                    "template": {"spec": {"containers": [{"name": "main", "image": image}]}},
                },
                "status": {"readyReplicas": ready},
            }
        )

    # -- time ------------------------------------------------------------------------------

    def tick(self, seconds: float = 1) -> None:
        self.now = self.now + timedelta(seconds=seconds)

    # -- the three programs ----------------------------------------------------------------

    def apply_feed(self) -> bool:
        """What `agent-registry serve` does with the feed file at start and on change."""
        return importer.apply_feed(self.store, self.feed_document, now=self.now)

    def reconcile(self) -> None:
        """The Registry Controller sees every Agent, and through it every AgentRelease."""
        for agent in self.cluster.list(AV, "Agent"):
            self.controller.reconcile_agent(agent)

    def provision(self) -> list[dict[str, Any]]:
        """One pass of the provisioner. Returns what it reported for each object it was asked
        to hold."""
        cancels = ProvisionerSessions(
            "http://testserver",
            types.SimpleNamespace(tokens=_Token("provisioner"), http=self.sessions_http),
        )
        assert run_pass(
            self.cluster,
            self.registry,
            namespace=PLATFORM_NAMESPACE,
            clock=lambda: self.now,
            sessions=cancels,
        )
        return [
            {"object": o.name, **o.document}
            for o in self.store.observations(CLUSTER, subject_kind="projection")
            if o.name != "report" and o.observed_at == self.now
        ]

    def settle(self) -> list[dict[str, Any]]:
        """The controller reacts to what is on the cluster, then the provisioner passes: it
        writes what the registry wants and reports what the controller concluded."""
        self.tick()
        self.reconcile()
        return self.provision()

    def evaluate(self, name: str = "evaluator-1") -> bool:
        """One cycle of the evaluator `name`: claim, run the suite through the session
        interface, post the verdict. True when it handled an evaluation. Time passes only by
        the evaluator's own sleeping, which moves the world's clock."""
        return self.worker(name).run_pass()

    def worker(self, name: str, *, sleep=None) -> Worker:
        """The evaluator `name`, made once: the real cycle, with the registry and the session
        interface reached over HTTP in process."""
        if name not in self.workers:
            tokens = _Token("evaluator")
            registry = EvaluatorRegistry("http://testserver", CLUSTER, tokens, self.http)
            sessions = EvaluatorSessions("http://testserver", tokens, self.sessions_http)
            run = Runtime(sessions, lambda: self.now.timestamp(), sleep or self.tick)
            self.workers[name] = Worker(name, registry, run)
        return self.workers[name]

    # -- the registry, as its callers reach it ---------------------------------------------

    def call(self, method: str, path: str, *, token: str = "operator", **kwargs: Any):
        return self.http.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )

    def read(self, path: str, *, token: str = "reader", **params: Any) -> Any:
        answer = self.call("GET", path, token=token, params=params)
        assert answer.status_code == 200, answer.text
        return answer.json()

    def write(self, path: str, body: dict[str, Any] | None = None) -> Any:
        answer = self.call("POST", path, json=body)
        assert answer.status_code == 200, answer.text
        return answer.json()

    def status(self) -> dict[str, Any]:
        return self.read("/v1/status", cluster=CLUSTER)

    def agent_status(self, agent: str) -> dict[str, Any]:
        return next(a for a in self.status()["agents"] if a["agent"] == agent)

    # -- the cluster, as tests read it -----------------------------------------------------

    def agent(self, name: str) -> dict[str, Any]:
        found = self.cluster.get(AV, "Agent", TENANT, name)
        assert found is not None
        return found

    def release(self, name: str) -> dict[str, Any] | None:
        return self.cluster.get(AV, "AgentRelease", TENANT, name)

    def provisioner_writes(self, since: int = 0) -> list[tuple[str, str | None, str]]:
        return [w for w in self.cluster.writes_by(FIELD_MANAGER, since) if w[0] != "ConfigMap"]

    # -- a release, as the agent's release workflow publishes it ---------------------------

    def publish(self, agent: str, version: str, build: str) -> dict[str, Any]:
        """Build, sign and describe a new release of an agent whose declaration did not change.

        Returns the body of `POST .../releases`. `build` is a hex digit: it makes the image
        digest and the build revision. The policy digest is the real one, compiled for this
        cluster as the release workflow compiles it."""
        declared, _ = committed(agent)
        release = {
            "apiVersion": AV,
            "kind": "AgentRelease",
            "metadata": {"name": f"{agent}-2026-11-18-{build * 8}", "namespace": TENANT},
            "spec": {
                "agent": agent,
                "image": f"registry.almckay.io/agents/{agent}@sha256:{build * 64}",
                "manifestSha256": build * 64,
                "policySha256": "",
                "lockSha256": build * 64,
                "buildRevision": build * 40,
            },
        }
        release["spec"]["policySha256"] = digest_hex(
            compile_bundle(
                declared, release, mcp_resource_base=MCP_RESOURCE_BASE, trust_domain=TRUST_DOMAIN
            )
        )
        tag = f"v{version}"
        self.signatures.sign(release["spec"]["image"], agent, tag)
        return {
            "agent": copy.deepcopy(declared),
            "agentRelease": release,
            "lineage": {"repository": REPOSITORY, "tag": f"{agent}/{tag}"},
        }

    def publish_declared(
        self,
        agent: str,
        version: str,
        build: str,
        *,
        suite: dict[str, Any] | None = None,
        change: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """A release as the release workflow reports it: it carries its declaration, the
        Agent's spec without `enabled` (delivery 2), and the evaluation suite if there is one.

        `change` edits the Agent's spec first, for a release that asks for something else; the
        policy digest is compiled for that Agent. Returns the body of `POST /v1/reports/releases`."""
        body = self.publish(agent, version, build)
        document = canonical.agent_document(body["agent"])
        if change is not None:
            change(document["spec"])
        release = body["agentRelease"]
        release["spec"]["declaration"] = {
            k: v for k, v in document["spec"].items() if k != "enabled"
        }
        release["spec"]["manifestSha256"] = canonical.digest(document)
        release["spec"]["policySha256"] = digest_hex(
            compile_bundle(
                document, release, mcp_resource_base=MCP_RESOURCE_BASE, trust_domain=TRUST_DOMAIN
            )
        )
        report: dict[str, Any] = {
            "agent": document,
            "agentRelease": release,
            "lineage": {"checksums": {"agent.yaml": "sha256:" + build * 64}},
        }
        if suite is not None:
            report["evaluation"] = {"suite": suite}
        return report

    def report_release(self, body: dict[str, Any], version: str) -> Any:
        """The release workflow's report: the route, with the identity of the workflow that ran
        at the tag `<agent>/v<version>`. Returns the answer."""
        agent = body["agent"]["metadata"]["name"]
        tag = f"{agent}/v{version}"
        workflow = f"{REPOSITORY}/.github/workflows/release.yml@refs/tags/{tag}"
        name = f"github-{tag}"
        self.github._tokens[name] = GithubClaims(
            repository=REPOSITORY,
            ref=f"refs/tags/{tag}",
            ref_type="tag",
            sha="c" * 40,
            job_workflow_ref=workflow,
        )
        return self.http.post(
            "/v1/reports/releases", json=body, headers={"Authorization": f"Bearer {name}"}
        )

    def revoke(self, release: str, *, reason: str = "security", sessions: str | None = None) -> str:
        """A reviewed revocation of an agent release reaches the feed (delivery 8.1), and the
        registry applies it. Returns the revocation's id."""
        entry = {
            "schemaVersion": 1,
            "id": f"{self.now:%Y-%m-%d}-db-admin-{release[-8:]}",
            "kind": "agent",
            "item": "db-admin",
            "release": release,
            "reason": reason,
            "summary": "loop test",
            "revokedBy": "al",
            "revokedAt": f"{self.now:%Y-%m-%dT%H:%M:%SZ}",
        }
        if sessions is not None:
            entry["sessions"] = sessions
        listed = [*self.feed_document.get("revocations", []), entry]
        self.feed_document["revocations"] = sorted(listed, key=lambda e: e["id"])
        assert self.apply_feed()
        return entry["id"]

    def start_session(self, agent: str, request: str, *, token: str = "operator") -> Any:
        """A person starts a session of the agent's current release, through the session
        interface."""
        return self.sessions_http.post(
            f"/v1/tenants/{TENANT}/sessions",
            json={"agent": agent, "task": {"request": request}},
            headers={"Authorization": f"Bearer {token}"},
        )
