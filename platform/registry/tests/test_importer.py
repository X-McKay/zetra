"""The importer: documents from files, the catalog feed, and what they write."""

from __future__ import annotations

import copy
import json
from datetime import timedelta

import pytest

from agent_registry import canonical, cli, defaults, derive, importer
from agent_registry.importer import (
    ImportReport,
    apply_feed,
    import_documents,
    index_observations,
    load_directory,
    load_feed,
)
from agent_registry.models import Item, Observation, deployment_id
from agent_registry.store.memory import MemoryStore
from tests.world import (
    CLUSTER,
    DEP,
    FIXTURES,
    NAMESPACE,
    T0,
    TENANT,
    World,
    fixture_documents,
)

MANIFESTS = FIXTURES / "manifests"
FEED = FIXTURES / "feed.json"
API = "agentplatform.io/v1alpha1"


def feed() -> dict:
    return json.loads(FEED.read_text())


def fresh(*, with_feed: bool = True) -> MemoryStore:
    store = MemoryStore(clock=lambda: T0)
    if with_feed:
        apply_feed(store, feed(), now=T0)
    return store


def run(store: MemoryStore, documents, **kwargs) -> ImportReport:
    return import_documents(store, documents, cluster=CLUSTER, now=T0, **kwargs)


def db_admin() -> list[dict]:
    agent, releases = fixture_documents()
    return [agent, *releases]


def new_release(name: str, *, approved_at: str | None, letter: str = "a") -> dict:
    """Another db-admin release, as a manifest in git."""
    spec = {
        "agent": "db-admin",
        "image": f"registry.almckay.io/agents/db-admin@sha256:{letter * 64}",
        "manifestSha256": letter * 64,
        "policySha256": letter * 64,
        "lockSha256": letter * 64,
        "buildRevision": letter * 40,
    }
    if approved_at:
        spec["approval"] = {
            "approved": True,
            "approvedBy": "al@example.com",
            "approvedAt": approved_at,
        }
    return {
        "apiVersion": API,
        "kind": "AgentRelease",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": spec,
    }


def everything(store: MemoryStore) -> dict:
    """All desired state of a store except its events."""

    def dump(rows):
        return [row.model_dump(mode="json") for row in rows]

    deployments = store.deployments()
    revisions = [r for d in deployments for r in store.revisions(d.id)]
    return {
        "clusters": dump(store.clusters()),
        "tenants": dump(store.tenants()),
        "repositories": dump(store.trusted_repositories()),
        "items": dump(store.items()),
        "releases": dump(store.releases()),
        "requirements": dump(store.requirements()),
        "deployments": dump(deployments),
        "revisions": dump(revisions),
        "resolutions": [dump(store.resolutions(r.id)) for r in revisions],
        "aliases": dump(store.aliases()),
        "approvals": dump(store.approvals()),
    }


def pinned(store: MemoryStore, number: int) -> None:
    with store.transaction("test") as tx:
        deployment = tx.deployment(DEP).model_copy(update={"pinned_revision": number})
        tx.put_deployment(deployment)
        tx.event("pinned", deployment_id=DEP)


# -- loading ---------------------------------------------------------------------------------


def test_load_directory_finds_the_agents_and_releases_of_the_fixtures():
    documents = load_directory(MANIFESTS)

    kinds = [d["kind"] for d in documents]
    assert (kinds.count("Agent"), kinds.count("AgentRelease"), len(kinds)) == (13, 23, 36)
    assert documents.problems == []


def test_load_directory_ignores_other_kinds_and_other_files(tmp_path):
    (tmp_path / "role.yaml").write_text("apiVersion: rbac.authorization.k8s.io/v1\nkind: Role\n")
    (tmp_path / "notes.txt").write_text("kind: Agent\n")
    (tmp_path / "other.yaml").write_text("apiVersion: example.com/v1\nkind: Agent\n")
    (tmp_path / "empty.yaml").write_text("")
    (tmp_path / "list.yaml").write_text("- kind: Agent\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "agent.yml").write_text(
        f"apiVersion: {API}\nkind: Agent\nmetadata: {{name: x, namespace: n}}\n"
    )

    documents = load_directory(tmp_path)

    assert [d["metadata"]["name"] for d in documents] == ["x"]
    assert documents.problems == []


def test_load_directory_reads_a_file_with_several_documents_whole(tmp_path):
    (tmp_path / "all.yaml").write_text(
        f"apiVersion: {API}\nkind: Agent\nmetadata: {{name: one, namespace: n}}\n---\n"
        "apiVersion: v1\nkind: ConfigMap\n---\n"
        f"apiVersion: {API}\nkind: AgentRelease\nmetadata: {{name: two, namespace: n}}\n"
    )

    documents = load_directory(tmp_path)

    assert [(d["kind"], d["metadata"]["name"]) for d in documents] == [
        ("Agent", "one"),
        ("AgentRelease", "two"),
    ]


def test_a_file_that_is_not_yaml_is_a_problem_with_its_path(tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("kind: [unclosed\n  - x: : :\n")
    (tmp_path / "good.yaml").write_text(
        f"apiVersion: {API}\nkind: Agent\nmetadata: {{name: ok, namespace: n}}\n"
    )

    documents = load_directory(tmp_path)

    assert [d["metadata"]["name"] for d in documents] == ["ok"]
    assert len(documents.problems) == 1
    assert str(broken) in documents.problems[0]
    report = run(fresh(), documents)
    assert report.problems[0] == documents.problems[0]


# -- the feed ----------------------------------------------------------------------------------


def test_the_feed_writes_every_record_class():
    store = fresh(with_feed=False)

    assert apply_feed(store, feed(), now=T0) is True

    assert [c.name for c in store.clusters()] == [CLUSTER]
    tenant = store.tenant(CLUSTER, TENANT)
    assert tenant.policy_revision == canonical.digest(feed()["tenants"][0])
    assert tenant.document == feed()["tenants"][0]
    (repository,) = store.trusted_repositories()
    assert repository.repository == "X-McKay/zetra-agents" and len(repository.agents) == 13
    assert [i.name for i in store.items("tool-server")] == [
        "github-mcp",
        "graphiti-mcp",
        "kubernetes-mcp",
        "postgres-admin-mcp",
        "postgres-mcp",
    ]
    assert [i.name for i in store.items("model")] == ["embeddings", "fast", "main"]
    assert not any(i.retired for i in store.items())
    assert {r.name for r in store.releases("tool-server")} == {
        "v2.0.2",
        "1.1.1-standalone",
        "v0.0.67",
        "sha256-dbbd346860d29f1543e991f30f3284bf4ab5f096d049ecc3426528f20b1b6e6b",
        "0.3.0",
    }
    assert store.requirements() == [] and store.provisions() == []
    server = store.deployment("tool-server:kubani:-:postgres-admin-mcp")
    assert (server.tenant, server.authority, server.namespace) == (None, "pull-request", "mcp")
    assert server.document == {
        "namespace": "mcp",
        "workload": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "postgres-admin-mcp"},
        "image": feed()["toolServers"][3]["image"],
        "pinnedByDigest": True,
        "registered": True,
        "backend": "postgres-admin",
    }
    release = store.release(
        "tool-server:postgres-admin-mcp:"
        + "sha256-"
        + "dbbd346860d29f1543e991f30f3284bf4ab5f096d049ecc3426528f20b1b6e6b"
    )
    assert (
        release.digest == "sha256:dbbd346860d29f1543e991f30f3284bf4ab5f096d049ecc3426528f20b1b6e6b"
    )
    assert (release.origin, release.lineage) == ("adopted", {"source": "crystaldba/postgres-mcp"})
    assert release.document == {"image": feed()["toolServers"][3]["image"]}
    assert store.release("tool-server:github-mcp:v2.0.2").digest == ""
    engine = store.deployment("model:kubani:-:main")
    assert engine.document["servedName"] == "Qwen3.6-35B-A3B-NVFP4"
    assert engine.document["backend"] == "llm-main"
    assert set(engine.document) == {
        "namespace",
        "workload",
        "servedName",
        "servedNameFrom",
        "backend",
    }
    model = store.release("model:main:Qwen3.6-35B-A3B-NVFP4")
    assert (model.digest, model.origin, model.document) == (
        "",
        "adopted",
        {"servedName": "Qwen3.6-35B-A3B-NVFP4"},
    )
    assert store.release("model:fast:Qwen-Qwen3.5-0.8B") is not None
    assert [(a.alias, a.tenant, a.deployment_id, a.backend) for a in store.aliases()] == [
        ("fast", None, "model:kubani:-:fast", "llm-fast"),
        ("main", None, "model:kubani:-:main", "llm-main"),
    ]
    assert store.revisions("tool-server:kubani:-:github-mcp") == []
    (event,) = store.events()
    assert (event.type, event.actor, event.deployment_id) == ("feed-applied", "feed", None)
    assert "tool-server:kubernetes-mcp" in event.detail["changed"]
    assert f"tenant:{CLUSTER}/{TENANT}" in event.detail["changed"]
    assert "model:main" in event.detail["changed"]


def test_a_second_feed_apply_changes_nothing():
    store = fresh()
    before, generation = everything(store), store.generation()

    assert apply_feed(store, feed(), now=T0 + timedelta(hours=1)) is False

    assert everything(store) == before
    assert store.generation() == generation
    assert len(store.events()) == 1


def test_a_changed_tenant_policy_changes_the_policy_revision():
    store = fresh()
    old = store.tenant(CLUSTER, TENANT).policy_revision
    changed = feed()
    changed["tenants"][0]["policy"]["vulnExceptions"] = ["CVE-2026-0001"]

    assert apply_feed(store, changed, now=T0) is True

    tenant = store.tenant(CLUSTER, TENANT)
    assert tenant.policy_revision == canonical.digest(changed["tenants"][0]) != old
    event = store.events()[-1]
    assert len(store.events()) == 2
    assert event.detail == {"changed": [f"tenant:{CLUSTER}/{TENANT}"]}


def test_a_new_image_adds_a_release_and_keeps_the_old_one():
    store = fresh()
    changed = feed()
    changed["toolServers"][2]["image"] = "quay.io/containers/kubernetes_mcp_server:v0.0.68"

    assert apply_feed(store, changed, now=T0) is True

    names = {r.name for r in store.releases("tool-server", "kubernetes-mcp")}
    assert names == {"v0.0.67", "v0.0.68"}
    deployment = store.deployment("tool-server:kubani:-:kubernetes-mcp")
    assert deployment.document["image"].endswith("v0.0.68")
    assert store.events()[-1].detail == {"changed": ["tool-server:kubernetes-mcp"]}
    changed["modelEngines"][1]["servedName"] = "Qwen/Next"
    apply_feed(store, changed, now=T0)
    assert {r.name for r in store.releases("model", "fast")} == {"Qwen-Qwen3.5-0.8B", "Qwen-Next"}


def test_what_the_feed_drops_is_retired_and_comes_back_when_it_returns():
    store = fresh()
    smaller = feed()
    del smaller["toolServers"][0]
    del smaller["modelEngines"][2]

    assert apply_feed(store, smaller, now=T0) is True

    assert store.item("tool-server", "github-mcp").retired is True
    assert store.item("model", "embeddings").retired is True
    assert store.item("tool-server", "graphiti-mcp").retired is False
    assert store.deployment("tool-server:kubani:-:github-mcp") is not None
    assert store.release("tool-server:github-mcp:v2.0.2") is not None
    assert apply_feed(store, smaller, now=T0) is False

    assert apply_feed(store, feed(), now=T0) is True
    assert not any(i.retired for i in store.items())


def test_the_feed_updates_a_trusted_repository_and_a_cluster():
    store = fresh()
    changed = feed()
    changed["trustedRepositories"][0]["agents"].append("new-agent")
    changed["clusters"][0]["gateway"] = "http://elsewhere"

    assert apply_feed(store, changed, now=T0) is True

    assert "new-agent" in store.trusted_repositories()[0].agents
    assert store.cluster(CLUSTER).gateway == "http://elsewhere"
    assert store.events()[-1].detail == {
        "changed": ["cluster:kubani", "trusted-repository:X-McKay/zetra-agents"]
    }


def test_the_feed_keeps_the_pointers_of_an_infrastructure_deployment():
    store = fresh()
    changed = feed()
    changed["toolServers"][0]["description"] = "another"

    apply_feed(store, changed, now=T0)

    assert store.item("tool-server", "github-mcp").description == "another"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda f: f.update(version=2), "version"),
        (lambda f: f.pop("version"), "version"),
        (lambda f: f["clusters"][0].pop("gateway"), "gateway"),
        (lambda f: f["tenants"][0].update(cluster="nowhere"), "nowhere"),
        (lambda f: f["trustedRepositories"][0].update(tenant="nobody"), "nobody"),
        (lambda f: f["toolServers"][0].pop("registered"), "registered"),
        (lambda f: f["aliases"][0].update(engine="ghost"), "ghost"),
        (lambda f: f["aliases"][0].update(cluster="other"), "other"),
        (lambda f: f.update(aliases={}), "aliases"),
    ],
)
def test_a_feed_that_fails_a_check_is_refused_and_nothing_is_written(change, message):
    bad = feed()
    change(bad)
    store = MemoryStore()

    with pytest.raises(ValueError, match=message):
        apply_feed(store, bad)

    assert store.generation() == 0 and store.clusters() == []


def test_load_feed_checks_the_file(tmp_path):
    assert load_feed(FEED) == feed()
    bad = tmp_path / "feed.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError):
        load_feed(bad)
    bad.write_text("[]")
    with pytest.raises(ValueError):
        load_feed(bad)
    bad.write_text(json.dumps({**feed(), "version": 7}))
    with pytest.raises(ValueError, match="version"):
        load_feed(bad)


# -- importing the fixtures ----------------------------------------------------------------------


def test_the_fixtures_import_as_the_contract_says():
    store = fresh()

    report = run(store, load_directory(MANIFESTS))

    assert (report.agents, report.releases) == (13, 23)
    assert (report.orphans, report.skipped, report.problems) == ([], [], [])
    assert report.changed is True and report.updated == []
    assert len(store.items("agent")) == 13
    deployments = store.deployments(kind="agent")
    assert len(deployments) == 13
    assert {d.authority for d in deployments} == {"pull-request"}
    assert all(d.tenant == TENANT and d.namespace == NAMESPACE for d in deployments)
    assert len(store.releases("agent")) == 23
    revisions = [r for d in deployments for r in store.revisions(d.id)]
    assert len(revisions) == 23 and len(store.approvals()) == 23
    assert {(r.origin, r.phase, r.generation) for r in revisions} == {("imported", "activated", 0)}
    assert all(r.approval_id == f"imported:{r.deployment_id}@{r.number}" for r in revisions)
    for deployment in deployments:
        numbers = [r.number for r in store.revisions(deployment.id)]
        assert numbers == list(range(1, len(numbers) + 1))
        assert deployment.current_revision == numbers[-1]
        assert deployment.pinned_revision is None and deployment.generation == 0
    assert len(report.created) == 13 + 13 + 23 + 23 + 23
    assert "agent:db-admin" in report.created and DEP in report.created
    events = [e for e in store.events() if e.type == "agent-imported"]
    assert len(events) == 13 and all(e.actor == "importer" for e in events)


def test_the_source_of_an_item_is_the_trusted_repository_that_lists_it():
    store = fresh()
    documents = load_directory(MANIFESTS)
    unlisted = copy.deepcopy(next(d for d in documents if d["kind"] == "Agent"))
    unlisted["metadata"]["name"] = "stranger"

    run(store, [*documents, unlisted])

    assert store.item("agent", "db-admin").source == "X-McKay/zetra-agents"
    assert store.item("agent", "stranger").source == ""


def test_db_admin_in_detail():
    store = fresh()
    run(store, load_directory(MANIFESTS))

    deployment = store.deployment(DEP)
    assert (deployment.current_revision, deployment.enabled) == (2, True)
    assert deployment.document == {"agent": canonical.agent_document(fixture_documents()[0])}
    first, second = store.revisions(DEP)
    assert first.release_id == "agent:db-admin:db-admin-2026-11-07-3e94dc5e"
    assert second.release_id == "agent:db-admin:db-admin-2026-11-13-33c8acb2"
    tenant = store.tenant(CLUSTER, TENANT)
    for revision, approved_at in ((first, "2026-11-07T02:04:48"), (second, "2026-11-13T21:58:12")):
        approval = store.approval(f"imported:{DEP}@{revision.number}")
        assert revision.approval_id == approval.id and revision.phase == "activated"
        assert revision.activated_at == approval.approved_at
        assert approval.approved_at.isoformat() == approved_at + "+00:00"
        assert approval.origin == "imported" and approval.state == "active"
        assert approval.approved_by == "4785437+X-McKay@users.noreply.github.com"
        assert approval.ticket is None and approval.base_authority_digest == ""
        assert approval.authority_digest == revision.authority_digest
        assert approval.policy_revision == revision.policy_revision == tenant.policy_revision
        assert revision.authority_digest == canonical.authority_digest(revision.authority)
        assert revision.authority == canonical.authority_of(deployment.document["agent"])
        assert revision.created_at == T0 and revision.reason == ""
    assert second.agent_document == deployment.document["agent"]
    block = fixture_documents()[1][1]["spec"]["approval"]
    assert store.approval(f"imported:{DEP}@2").evidence == {"approval": block}
    release = store.release(second.release_id)
    assert "approval" not in release.document["spec"] and release.origin == "built"
    assert release.digest.startswith("sha256:33c8acb2")
    assert [r.target for r in store.requirements(release.id)] == ["postgres-admin", "llm-main"][
        ::-1
    ]
    by_target = {r.target: r for r in store.resolutions(second.id)}
    assert by_target["postgres-admin"].resolved_deployment == (
        "tool-server:kubani:-:postgres-admin-mcp"
    )
    assert by_target["llm-main"].resolved_deployment == "model:kubani:-:main"
    assert by_target["llm-main"].detail == {"alias": "main", "servedName": "Qwen3.6-35B-A3B-NVFP4"}
    (event,) = [e for e in store.events(deployment_id=DEP)]
    assert event.type == "agent-imported"
    assert f"{DEP}@2" in event.detail["created"] and event.detail["updated"] == []


def test_a_requirement_that_nothing_provides_resolves_to_nothing():
    store = fresh()
    run(store, load_directory(MANIFESTS))

    deployment = store.deployment("agent:kubani:tenant-test:conformance-budget-day")
    (revision,) = store.revisions(deployment.id)
    rows = {r.target: r for r in store.resolutions(revision.id)}
    assert rows["llm-budget-conformance"].resolved_deployment is None


def test_importing_db_admin_gives_what_the_test_world_holds():
    """`World.seed()` is built by hand, with a feed that has no tool servers or engines, so the
    feed given here has none either; everything else is compared whole."""
    world = World()
    world.seed()
    bare = {**feed(), "toolServers": [], "modelEngines": [], "aliases": []}
    store = MemoryStore(clock=lambda: T0)
    apply_feed(store, bare, now=T0)

    report = run(store, db_admin())

    assert report.changed
    mine, theirs = everything(store), everything(world.store)
    assert mine == theirs
    assert [e.type for e in store.events(deployment_id=DEP)] == ["agent-imported"]


def test_importing_db_admin_after_the_full_feed_resolves_its_requirements():
    world = World()
    world.seed()
    store = fresh()
    run(store, db_admin())

    mine, theirs = everything(store), everything(world.store)
    different = {k for k in mine if mine[k] != theirs[k]}

    # The test world's feed has no tool servers, engines or aliases, so every difference is
    # one of those records, or the resolutions that now find them.
    assert different == {"items", "releases", "deployments", "aliases", "resolutions"}
    for key in ("tenants", "revisions", "approvals", "requirements"):
        assert mine[key] == theirs[key]
    agent_rows = lambda rows: [r for r in rows if r.get("kind", "agent") == "agent"]  # noqa: E731
    assert agent_rows(mine["items"]) == agent_rows(theirs["items"])
    assert agent_rows(mine["releases"]) == agent_rows(theirs["releases"])
    assert [d for d in mine["deployments"] if d["kind"] == "agent"] == theirs["deployments"]


# -- idempotence ---------------------------------------------------------------------------------


def test_the_same_input_a_second_time_changes_nothing():
    store = fresh()
    documents = load_directory(MANIFESTS)
    run(store, documents)
    before, generation, events = everything(store), store.generation(), len(store.events())

    assert apply_feed(store, feed(), now=T0 + timedelta(minutes=5)) is False
    second = import_documents(
        store, documents, cluster=CLUSTER, now=T0 + timedelta(minutes=5), actor="again"
    )

    assert second.changed is False
    assert (second.created, second.updated, second.problems) == ([], [], [])
    assert (second.agents, second.releases) == (13, 23)
    assert everything(store) == before
    assert store.generation() == generation and len(store.events()) == events


# -- what is not imported -------------------------------------------------------------------------


def test_a_release_without_its_agent_is_an_orphan_and_is_not_stored():
    store = fresh()
    agent, releases = fixture_documents()
    others = [d for d in load_directory(MANIFESTS) if (d["metadata"]["name"] != "db-admin")]
    others = [d for d in others if d.get("spec", {}).get("agent") != "db-admin"]
    documents = [*others, *releases]

    report = run(store, documents)

    assert report.orphans == [f"{NAMESPACE}/{r['metadata']['name']}" for r in releases]
    assert report.agents == 12 and report.releases == 23
    assert store.item("agent", "db-admin") is None and store.releases(item="db-admin") == []
    assert store.deployment(DEP) is None
    assert len(store.deployments(kind="agent")) == 12
    assert report.problems == []
    assert not any(DEP in line for line in report.created)


def test_a_release_in_another_namespace_is_not_the_agents():
    store = fresh()
    agent, releases = fixture_documents()
    stray = copy.deepcopy(releases[0])
    stray["metadata"]["namespace"] = "elsewhere"

    report = run(store, [agent, stray])

    assert report.orphans == ["elsewhere/db-admin-2026-11-07-3e94dc5e"]
    assert store.revisions(DEP) == []


def test_a_document_in_a_namespace_no_tenant_has_is_a_problem():
    store = fresh()
    agent, releases = fixture_documents()
    stray = copy.deepcopy(agent)
    stray["metadata"]["namespace"] = "elsewhere"

    report = run(store, [stray])

    assert len(report.problems) == 1 and "elsewhere/db-admin" in report.problems[0]
    assert report.created == [] and store.generation() == 1


def test_an_agent_the_registry_owns_is_skipped_even_when_its_documents_differ():
    world = World()
    world.seed()
    world.take()
    agent, releases = fixture_documents()
    agent = copy.deepcopy(agent)
    agent["spec"]["description"] = "something else entirely"
    agent["spec"]["enabled"] = False
    extra = new_release("db-admin-2026-11-20-aaaaaaaa", approved_at="2026-11-20T00:00:00Z")
    before, generation = everything(world.store), world.store.generation()

    report = run(world.store, [agent, *releases, extra])

    assert report.skipped == [DEP]
    assert (report.created, report.updated, report.problems) == ([], [], [])
    assert report.changed is False and (report.agents, report.releases) == (1, 3)
    assert everything(world.store) == before
    assert world.store.generation() == generation


# -- changes -------------------------------------------------------------------------------------


def test_a_changed_agent_document_updates_the_item_and_the_deployment():
    store = fresh()
    agent, releases = db_admin()[0], db_admin()[1:]
    run(store, [agent, *releases])
    changed = copy.deepcopy(agent)
    changed["spec"]["description"] = "A different description"
    events = len(store.events())

    report = run(store, [changed, *releases])

    assert report.updated == ["agent:db-admin", DEP] and report.created == []
    assert store.item("agent", "db-admin").description == "A different description"
    deployment = store.deployment(DEP)
    assert deployment.document["agent"]["spec"]["description"] == "A different description"
    assert (deployment.current_revision, deployment.generation) == (2, 0)
    assert len(store.revisions(DEP)) == 2
    assert len(store.events()) == events + 1
    event = store.events()[-1]
    assert (event.type, event.deployment_id) == ("agent-imported", DEP)
    assert event.detail == {"created": [], "updated": ["agent:db-admin", DEP]}


def test_an_agent_that_is_disabled_in_git_is_disabled_here():
    store = fresh()
    agent, releases = db_admin()[0], db_admin()[1:]
    run(store, [agent, *releases])
    disabled = copy.deepcopy(agent)
    disabled["spec"]["enabled"] = False

    report = run(store, [disabled, *releases])

    assert report.updated == [DEP]
    assert store.deployment(DEP).enabled is False
    assert len(store.events()) == 3
    run(store, [agent, *releases])
    assert store.deployment(DEP).enabled is True


def test_a_release_added_later_is_the_next_revision():
    store = fresh()
    agent, releases = db_admin()[0], db_admin()[1:]
    run(store, [agent, *releases])
    added = new_release("db-admin-2026-11-20-aaaaaaaa", approved_at="2026-11-20T08:00:00Z")

    report = run(store, [agent, *releases, added])

    release_id = "agent:db-admin:db-admin-2026-11-20-aaaaaaaa"
    assert report.created == [release_id, f"{DEP}@3", f"imported:{DEP}@3"]
    assert report.updated == [DEP]
    third = store.revision(DEP, 3)
    assert third.release_id == release_id and third.phase == "activated"
    assert store.deployment(DEP).current_revision == 3
    assert (
        store.approval(f"imported:{DEP}@3").approved_at.isoformat() == "2026-11-20T08:00:00+00:00"
    )
    assert store.revision(DEP, 1).release_id.endswith("3e94dc5e")


def test_an_unapproved_release_is_a_revision_that_is_not_current_until_it_is_approved():
    store = fresh()
    agent, releases = db_admin()[0], db_admin()[1:]
    run(store, [agent, *releases])
    waiting = new_release("db-admin-2026-11-20-bbbbbbbb", approved_at=None, letter="b")

    report = run(store, [agent, *releases, waiting])

    assert report.created == [
        "agent:db-admin:db-admin-2026-11-20-bbbbbbbb",
        f"{DEP}@3",
    ]
    assert report.updated == []
    third = store.revision(DEP, 3)
    assert (third.phase, third.reason, third.approval_id) == (
        "awaiting-runtime",
        "imported unapproved",
        None,
    )
    assert third.activated_at is None
    assert store.deployment(DEP).current_revision == 2
    assert store.resolutions(third.id) != []

    approved = new_release(
        "db-admin-2026-11-20-bbbbbbbb", approved_at="2026-11-21T09:00:00Z", letter="b"
    )
    approved["spec"]["approval"]["ticket"] = "OPS-7"
    report = run(store, [agent, *releases, approved])

    assert report.created == [f"imported:{DEP}@3"] and report.updated == [DEP]
    third = store.revision(DEP, 3)
    assert (third.phase, third.reason, third.approval_id) == (
        "activated",
        "",
        f"imported:{DEP}@3",
    )
    assert third.activated_at.isoformat() == "2026-11-21T09:00:00+00:00"
    approval = store.approval(third.approval_id)
    assert approval.ticket == "OPS-7" and approval.authority_digest == third.authority_digest
    assert approval.evidence == {"approval": approved["spec"]["approval"]}
    assert store.deployment(DEP).current_revision == 3
    assert run(store, [agent, *releases, approved]).changed is False


def test_releases_are_numbered_approved_first_by_time_then_by_name():
    store = fresh()
    agent = db_admin()[0]
    documents = [
        new_release("db-admin-z", approved_at=None, letter="1"),
        new_release("db-admin-b", approved_at="2026-11-02T00:00:00Z", letter="2"),
        new_release("db-admin-c", approved_at="2026-11-01T00:00:00Z", letter="3"),
        new_release("db-admin-a", approved_at=None, letter="4"),
        new_release("db-admin-d", approved_at="2026-11-02T00:00:00Z", letter="5"),
    ]

    run(store, [agent, *documents])

    order = [store.release(r.release_id).name for r in store.revisions(DEP)]
    assert order == ["db-admin-c", "db-admin-b", "db-admin-d", "db-admin-a", "db-admin-z"]
    assert store.deployment(DEP).current_revision == 3


def test_a_pinned_deployment_keeps_its_pointer():
    store = fresh()
    agent, releases = db_admin()[0], db_admin()[1:]
    run(store, [agent, *releases])
    pinned(store, 1)
    added = new_release("db-admin-2026-11-20-aaaaaaaa", approved_at="2026-11-20T08:00:00Z")

    report = run(store, [agent, *releases, added])

    deployment = store.deployment(DEP)
    assert (deployment.pinned_revision, deployment.current_revision) == (1, 2)
    assert len(store.revisions(DEP)) == 3 and report.created
    assert DEP not in report.updated


def test_an_existing_deployment_keeps_its_pointers_and_generation():
    store = fresh()
    agent, releases = db_admin()[0], db_admin()[1:]
    run(store, [agent, *releases])
    with store.transaction("test") as tx:
        tx.put_deployment(tx.deployment(DEP).model_copy(update={"generation": 41}))
        tx.event("generation", deployment_id=DEP)
    changed = copy.deepcopy(agent)
    changed["spec"]["description"] = "again"

    run(store, [changed, *releases])

    deployment = store.deployment(DEP)
    assert (deployment.generation, deployment.current_revision) == (41, 2)


def test_a_release_stored_with_another_document_is_a_problem_and_is_skipped():
    store = fresh()
    agent, releases = db_admin()[0], db_admin()[1:]
    stored = copy.deepcopy(releases[0])
    stored["spec"]["buildRevision"] = "f" * 40
    # A release of this id that is not a revision of any deployment, with another document.
    with store.transaction("test") as tx:
        tx.put_item(Item(kind="agent", name="db-admin"))
        tx.add_release(derive.agent_release_record(stored, recorded_at=T0))
        tx.event("test")

    report = run(store, [agent, *releases])

    assert len(report.problems) == 1
    assert "db-admin-2026-11-07-3e94dc5e" in report.problems[0]
    assert [r.release_id for r in store.revisions(DEP)] == [
        "agent:db-admin:db-admin-2026-11-13-33c8acb2"
    ]
    assert store.deployment(DEP).current_revision == 1


def test_approval_without_a_time_is_a_problem_for_that_release_only():
    store = fresh()
    agent, releases = db_admin()[0], db_admin()[1:]
    broken = copy.deepcopy(releases[0])
    del broken["spec"]["approval"]["approvedAt"]

    report = run(store, [agent, broken, releases[1]])

    assert len(report.problems) == 1 and "AgentRelease" in report.problems[0]
    assert [r.number for r in store.revisions(DEP)] == [1]
    assert store.deployment(DEP).current_revision == 1


def test_documents_removed_from_git_are_left_as_they_are():
    store = fresh()
    run(store, load_directory(MANIFESTS))
    before = everything(store)

    report = run(store, [])

    assert report.changed is False and (report.agents, report.releases) == (0, 0)
    assert everything(store) == before


def test_the_defaults_the_cluster_fills_in_make_no_difference():
    agent, releases = db_admin()[0], db_admin()[1:]
    bare = copy.deepcopy(agent)
    bare["spec"].pop("enabled", None)
    filled = copy.deepcopy(agent)
    filled["spec"] = defaults.agent_spec(filled["spec"])
    assert filled["spec"] != bare["spec"]

    store = fresh()
    run(store, [bare, *releases])
    before = everything(store)
    report = run(store, [filled, *releases])
    assert (report.changed, everything(store)) == (False, before)

    other = fresh()
    run(other, [filled, *releases])
    assert everything(other) == before
    again = run(other, [bare, *releases])
    assert again.changed is False


# -- the cluster's side ------------------------------------------------------------------------------


def observed_documents(store: MemoryStore) -> list[dict]:
    """The stored observations of agents and releases, as documents."""
    documents = []
    for seen in store.observations(CLUSTER):
        if seen.subject_kind in ("agent", "agentrelease") and seen.present:
            kind = "Agent" if seen.subject_kind == "agent" else "AgentRelease"
            documents.append({"apiVersion": API, "kind": kind, **copy.deepcopy(seen.document)})
    return documents


def test_indexing_observations_is_importing_the_observed_documents():
    world = World()
    world.seed()
    world.cluster_shows_everything()
    documents = observed_documents(world.store)
    assert len(documents) == 3
    by_hand, indexed = fresh(), fresh()
    for store in (by_hand, indexed):
        for observation in world.store.observations(CLUSTER):
            store.put_observations(CLUSTER, [observation])

    expected = run(by_hand, documents)
    report = index_observations(indexed, CLUSTER, now=T0)

    assert report == expected and report.changed is True
    assert (report.agents, report.releases) == (1, 2)
    assert everything(indexed) == everything(by_hand)
    assert indexed.events()[-1].actor == "indexer"
    mine, theirs = everything(indexed), everything(world.store)
    assert [d for d in mine["deployments"] if d["kind"] == "agent"] == theirs["deployments"]
    assert mine["revisions"] == theirs["revisions"] and mine["approvals"] == theirs["approvals"]
    assert indexed.deployment(DEP).current_revision == 2
    again = index_observations(indexed, CLUSTER, now=T0)
    assert again.changed is False and len(indexed.events()) == len(by_hand.events())


def test_indexing_follows_what_the_controller_says_is_ready():
    world = World()
    world.seed()
    world.cluster_shows_agent()
    world.cluster_shows(1, ready=True)
    world.cluster_shows(2, ready=False)
    store = fresh()
    for observation in world.store.observations(CLUSTER):
        store.put_observations(CLUSTER, [observation])

    report = index_observations(store, CLUSTER, now=T0)

    assert report.changed is True
    assert [r.number for r in store.revisions(DEP)] == [1, 2]
    assert store.deployment(DEP).current_revision == 1
    world.cluster_shows(2, ready=True)
    store.put_observations(
        CLUSTER,
        [
            world.store.observation(
                CLUSTER, "agentrelease", NAMESPACE, "db-admin-2026-11-13-33c8acb2"
            )
        ],
    )
    index_observations(store, CLUSTER, now=T0)
    assert store.deployment(DEP).current_revision == 2


def test_a_revision_whose_release_is_not_among_the_documents_does_not_count():
    store = fresh()
    agent, releases = db_admin()[0], db_admin()[1:]
    run(store, [agent, *releases])
    first = {
        **copy.deepcopy(releases[0]),
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }

    run(store, [agent, first])

    assert store.deployment(DEP).current_revision == 1


def test_a_release_that_is_not_ready_does_not_count_when_documents_carry_status():
    store = fresh()
    agent, releases = db_admin()[0], db_admin()[1:]
    ready = lambda value: {"conditions": [{"type": "Ready", "status": value}]}  # noqa: E731
    documents = [
        agent,
        {**releases[0], "status": ready("True")},
        {**releases[1], "status": ready("False")},
    ]

    run(store, documents)
    assert [r.number for r in store.revisions(DEP)] == [1, 2]
    assert store.deployment(DEP).current_revision == 1

    documents[2] = {**releases[1], "status": ready("True")}
    run(store, documents)
    assert store.deployment(DEP).current_revision == 2

    documents[2] = {**releases[1], "status": ready("False")}
    documents[1] = {**releases[0], "status": ready("False")}
    run(store, documents)
    assert store.deployment(DEP).current_revision == 2  # none qualifies: the pointer stays


def test_an_observation_that_is_not_present_is_not_imported_and_nothing_is_removed():
    world = World()
    world.seed()
    world.cluster_shows_everything()
    store = fresh()
    for observation in world.store.observations(CLUSTER):
        store.put_observations(CLUSTER, [observation])
    index_observations(store, CLUSTER, now=T0)
    before = everything(store)
    # The provisioner looks again and finds nothing in the namespace.
    store.put_observations(
        CLUSTER,
        [world.store.observations(CLUSTER)[0].model_copy(update={"observed_at": T0})],
        complete_for=[("agent", NAMESPACE), ("agentrelease", NAMESPACE)],
    )
    assert not any(o.present for o in store.observations(CLUSTER, subject_kind="agentrelease"))

    report = index_observations(store, CLUSTER, now=T0 + timedelta(minutes=1))

    assert report.changed is False
    assert everything(store) == before

    empty = fresh()
    gone = Observation(
        cluster=CLUSTER,
        subject_kind="agent",
        namespace=NAMESPACE,
        name="db-admin",
        observed_at=T0,
        present=False,
        document=world.store.observation(CLUSTER, "agent", NAMESPACE, "db-admin").document,
    )
    empty.put_observations(CLUSTER, [gone])
    report = index_observations(empty, CLUSTER, now=T0)
    assert (report.agents, report.changed, empty.deployments(kind="agent")) == (0, False, [])


def test_observations_of_another_cluster_or_kind_are_not_indexed():
    world = World()
    world.seed()
    world.cluster_shows_everything()
    store = fresh()
    for observation in world.store.observations(CLUSTER):
        store.put_observations(CLUSTER, [observation])
    store.put_observations(
        "other",
        [
            Observation(
                cluster="other",
                subject_kind="agent",
                namespace=NAMESPACE,
                name="intruder",
                observed_at=T0,
                document={"metadata": {}, "spec": {}},
            )
        ],
    )

    report = index_observations(store, CLUSTER, now=T0)

    assert report.agents == 1 and store.item("agent", "intruder") is None


# -- the command ---------------------------------------------------------------------------------


def test_the_import_command_prints_the_report_and_exits_zero(monkeypatch, capsys):
    store = MemoryStore()
    monkeypatch.setattr(cli, "_store", lambda settings: store)
    argv = ["import", "--dir", str(MANIFESTS), "--feed", str(FEED), "--cluster", "kubani"]

    assert cli.main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["feedChanged"] is True
    assert (first["agents"], first["releases"]) == (13, 23)
    assert len(first["created"]) == 95 and first["updated"] == []
    assert (first["orphans"], first["skipped"], first["problems"]) == ([], [], [])
    assert set(first) == {
        "feedChanged",
        "agents",
        "releases",
        "created",
        "updated",
        "orphans",
        "skipped",
        "problems",
    }
    generation = store.generation()

    assert cli.main(argv) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["feedChanged"] is False
    assert (second["created"], second["updated"]) == ([], [])
    assert (second["agents"], second["releases"]) == (13, 23)
    assert store.generation() == generation
    assert store.deployment(DEP).current_revision == 2
    assert deployment_id("agent", CLUSTER, TENANT, "db-admin") == DEP


def test_the_import_command_exits_one_when_there_is_a_problem(monkeypatch, capsys, tmp_path):
    store = MemoryStore()
    monkeypatch.setattr(cli, "_store", lambda settings: store)
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    for path in (MANIFESTS / NAMESPACE / "db-admin").glob("*.yaml"):
        (manifests / path.name).write_text(path.read_text())
    (manifests / "broken.yaml").write_text("a: [unclosed\n")

    status = cli.main(
        ["import", "--dir", str(manifests), "--feed", str(FEED), "--cluster", "kubani"]
    )

    printed = json.loads(capsys.readouterr().out)
    assert status == 1
    assert len(printed["problems"]) == 1 and "broken.yaml" in printed["problems"][0]
    assert (printed["agents"], printed["releases"]) == (1, 2)
    assert store.deployment(DEP).current_revision == 2


def test_the_package_exports_what_the_command_calls():
    assert {n for n in dir(importer) if not n.startswith("_")} >= {
        "ImportReport",
        "apply_feed",
        "import_documents",
        "index_observations",
        "load_directory",
        "load_feed",
    }
