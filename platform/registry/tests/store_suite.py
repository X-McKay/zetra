"""The store suite. Every implementation of `agent_registry.store.Store` passes all of it.

A test module subclasses `StoreSuite` and supplies a `store` fixture that yields an empty
store: see `test_store_memory.py`. The numbers in the section comments are the rules in
`agent_registry/store/__init__.py`.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from agent_registry.models import (
    Alias,
    AliasMove,
    Approval,
    Assessment,
    Cluster,
    Deployment,
    Evaluation,
    Hold,
    Item,
    Observation,
    Provision,
    Release,
    Requirement,
    Resolution,
    Revision,
    Revocation,
    Tenant,
    TrustedRepository,
    deployment_id,
    evaluation_id,
    release_id,
    revision_id,
)
from agent_registry.store import Conflict, Missing, Store, Unevented

T0 = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
DEP = deployment_id("agent", "kubani", "tenant-test", "db-admin")
REL1 = release_id("agent", "db-admin", "db-admin-2026-11-13-33c8acb2")
REL2 = release_id("agent", "db-admin", "db-admin-2026-11-18-0a1b2c3d")


def cluster() -> Cluster:
    return Cluster(
        name="kubani",
        trust_domain="cluster.local",
        gateway="https://agents-gw.example",
        flux_inventories=[{"namespace": "flux-system", "name": "apps"}],
    )


def tenant(policy_revision: str = "a" * 64) -> Tenant:
    return Tenant(
        cluster="kubani",
        name="tenant-test",
        namespace="tenant-test",
        policy_revision=policy_revision,
        document={"name": "tenant-test"},
    )


def item(name: str = "db-admin", kind: str = "agent") -> Item:
    return Item(kind=kind, name=name, owner="someone@example.com")


def release(rid: str = REL1, **changes) -> Release:
    kind, item_name, name = rid.split(":")
    values = {
        "id": rid,
        "kind": kind,
        "item": item_name,
        "name": name,
        "digest": "sha256:" + "1" * 64,
        "origin": "built",
        "document": {"spec": {"image": "registry.example/agents/db-admin@sha256:" + "1" * 64}},
        "recorded_at": T0,
    }
    return Release(**(values | changes))


def deployment(**changes) -> Deployment:
    values = {
        "id": DEP,
        "kind": "agent",
        "item": "db-admin",
        "cluster": "kubani",
        "tenant": "tenant-test",
        "namespace": "tenant-test",
    }
    return Deployment(**(values | changes))


def revision(number: int = 1, rid: str = REL1, **changes) -> Revision:
    values = {
        "id": revision_id(DEP, number),
        "deployment_id": DEP,
        "number": number,
        "release_id": rid,
        "origin": "registry",
        "agent_document": {"spec": {"enabled": True}},
        "authority": {"identity": {"mode": "agent"}},
        "authority_digest": "b" * 64,
        "policy_revision": "a" * 64,
        "created_at": T0,
    }
    return Revision(**(values | changes))


def approval(aid: str = "approval-1", number: int = 1, **changes) -> Approval:
    values = {
        "id": aid,
        "deployment_id": DEP,
        "revision": number,
        "authority_digest": "b" * 64,
        "base_authority_digest": "c" * 64,
        "policy_revision": "a" * 64,
        "approved_by": "al",
        "approved_at": T0,
        "origin": "registry",
    }
    return Approval(**(values | changes))


def evaluation(attempt: int = 1, dep: str = DEP, number: int = 1, **changes) -> Evaluation:
    values = {
        "id": evaluation_id(dep, number, attempt),
        "deployment_id": dep,
        "revision": number,
        "attempt": attempt,
        "suite": {"cases": [{"name": "smoke"}]},
        "suite_digest": "e" * 64,
        "created_at": T0,
    }
    return Evaluation(**(values | changes))


def revocation(rid: str = "rev-1", **changes) -> Revocation:
    values = {
        "id": rid,
        "kind": "agent",
        "item": "db-admin",
        "release": "db-admin-2026-11-13-33c8acb2",
        "reason": "security",
        "sessions": "cancel",
        "summary": "leaks a token",
        "revoked_by": "al",
        "revoked_at": T0,
        "indexed_at": T0,
    }
    return Revocation(**(values | changes))


def observation(name: str, at: datetime = T0, **changes) -> Observation:
    values = {
        "cluster": "kubani",
        "subject_kind": "agentrelease",
        "namespace": "tenant-test",
        "name": name,
        "observed_at": at,
        "in_flux_inventory": True,
        "document": {"spec": {}, "status": {}},
    }
    return Observation(**(values | changes))


def assessment(aid: str = "asm-1", **changes) -> Assessment:
    values = {
        "id": aid,
        "cluster": "kubani",
        "alias": "fast",
        "pair": {"engine": "fast", "release": "r1"},
        "candidate": {"engine": "fast", "release": "r2"},
        "dependants": [{"deployment": DEP, "authority": "registry", "enabled": True}],
        "blocked": False,
        "digest": "sha256:" + "d" * 64,
        "created_by": "al@example.com",
        "created_at": T0,
    }
    return Assessment(**(values | changes))


def move(mid: str = "mv-1", **changes) -> AliasMove:
    values = {
        "id": mid,
        "assessment_id": "asm-1",
        "cluster": "kubani",
        "alias": "fast",
        "candidate": {"engine": "fast", "release": "r2"},
        "sessions": "finish",
        "evaluation_seconds": 600,
        "started_by": "al@example.com",
        "started_at": T0,
    }
    return AliasMove(**(values | changes))


def hold(mid: str = "mv-1", dep: str = DEP, **changes) -> Hold:
    return Hold(**({"move_id": mid, "deployment_id": dep} | changes))


def seed(store: Store) -> None:
    """A cluster, a tenant, the db-admin item, its deployment, one release and revision 1."""
    with store.transaction("test") as tx:
        tx.put_cluster(cluster())
        tx.put_tenant(tenant())
        tx.put_item(item())
        tx.put_deployment(deployment())
        tx.add_release(release())
        tx.add_revision(revision())
        tx.event("seeded")


class StoreSuite:
    # -- 1. changes happen in a transaction; reads see committed state ---------------------

    def test_empty_store(self, store: Store):
        assert store.meta().mode == "normal"
        assert store.generation() == 0
        assert store.clusters() == []
        assert store.items() == []
        assert store.deployments() == []
        assert store.events() == []
        assert store.deployment(DEP) is None
        assert store.revision(DEP, 1) is None
        store.ping()

    def test_commit_is_visible_after_the_block_and_not_before(self, store: Store):
        with store.transaction("test") as tx:
            tx.put_cluster(cluster())
            tx.event("cluster-put")
            assert tx.cluster("kubani") == cluster()  # a transaction reads its own writes
            assert store.cluster("kubani") is None  # nobody else does
        assert store.cluster("kubani") == cluster()

    def test_records_round_trip(self, store: Store):
        seed(store)
        repository = TrustedRepository(
            repository="example/agents",
            cluster="kubani",
            tenant="tenant-test",
            issuer="https://token.actions.githubusercontent.com",
            subject_reg_exp="^https://github\\.com/example/agents/",
            agents=["db-admin"],
        )
        model = deployment_id("model", "kubani", None, "main")
        alias = Alias(cluster="kubani", alias="main", deployment_id=model, backend="llm-main")
        with store.transaction("test") as tx:
            tx.put_trusted_repository(repository)
            tx.put_item(item("main", "model"))
            tx.put_deployment(
                Deployment(
                    id=model,
                    kind="model",
                    item="main",
                    cluster="kubani",
                    namespace="vllm",
                    document={"servedName": "Some/Model"},
                )
            )
            tx.put_alias(alias)
            tx.event("more")
        assert store.tenant("kubani", "tenant-test") == tenant()
        assert store.tenants("kubani") == [tenant()]
        assert store.tenants("elsewhere") == []
        assert store.trusted_repositories() == [repository]
        assert store.item("agent", "db-admin") == item()
        assert [i.name for i in store.items()] == ["db-admin", "main"]
        assert [i.name for i in store.items("model")] == ["main"]
        assert store.release(REL1) == release()
        assert store.releases(kind="agent", item="db-admin") == [release()]
        assert store.deployment(DEP) == deployment()
        assert store.revision(DEP, 1) == revision()
        assert store.aliases("kubani") == [alias]
        assert store.deployment(model).tenant is None
        assert store.deployment(model).document == {"servedName": "Some/Model"}

    def test_deployment_filters(self, store: Store):
        seed(store)
        with store.transaction("test") as tx:
            tx.put_item(item("investigator"))
            tx.put_deployment(
                deployment(
                    id=deployment_id("agent", "kubani", "tenant-test", "investigator"),
                    item="investigator",
                    authority="registry",
                )
            )
            tx.event("more")
        assert len(store.deployments()) == 2
        assert [d.item for d in store.deployments(authority="registry")] == ["investigator"]
        assert [d.item for d in store.deployments(authority="pull-request")] == ["db-admin"]
        assert len(store.deployments(kind="agent", cluster="kubani", tenant="tenant-test")) == 2
        assert store.deployments(kind="model") == []
        assert store.deployments(cluster="elsewhere") == []

    def test_put_deployment_replaces_the_record(self, store: Store):
        seed(store)
        changed = deployment(authority="registry", enabled=False, current_revision=1, generation=7)
        with store.transaction("test") as tx:
            tx.put_deployment(changed)
            tx.event("deployment-changed", deployment_id=DEP)
        assert store.deployment(DEP) == changed

    # -- 2. a change needs an event, and an event needs a change ---------------------------

    def test_change_without_event_is_refused_and_writes_nothing(self, store: Store):
        with pytest.raises(Unevented), store.transaction("test") as tx:
            tx.put_cluster(cluster())
        assert store.clusters() == []
        assert store.generation() == 0

    def test_event_without_change_is_refused_and_writes_nothing(self, store: Store):
        seed(store)
        before = store.generation()
        with pytest.raises(Unevented), store.transaction("test") as tx:
            tx.put_cluster(cluster())  # same content: not a change
            tx.event("nothing-happened")
        assert store.generation() == before
        assert [e.type for e in store.events()] == ["seeded"]

    def test_transaction_that_reads_only_needs_no_event(self, store: Store):
        seed(store)
        before = store.generation()
        with store.transaction("test") as tx:
            assert tx.deployment(DEP) is not None
            tx.put_item(item())  # same content
            tx.add_release(release())  # same content
        assert store.generation() == before

    # -- 3. an exception leaves the store as it was ---------------------------------------

    def test_exception_rolls_everything_back(self, store: Store):
        seed(store)
        before = store.generation()
        with pytest.raises(RuntimeError), store.transaction("test") as tx:
            tx.put_deployment(deployment(enabled=False))
            tx.add_release(release(REL2))
            tx.event("half-done", deployment_id=DEP)
            raise RuntimeError("stop")
        assert store.deployment(DEP).enabled is True
        assert store.release(REL2) is None
        assert store.generation() == before
        assert [e.type for e in store.events()] == ["seeded"]

    def test_store_error_inside_a_transaction_rolls_back(self, store: Store):
        seed(store)
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.put_deployment(deployment(enabled=False))
            tx.event("half-done", deployment_id=DEP)
            tx.add_release(release(digest="sha256:" + "2" * 64))
        assert store.deployment(DEP).enabled is True

    # -- 4. write transactions do not interleave ------------------------------------------

    def test_transactions_are_serialised(self, store: Store):
        seed(store)
        inside = threading.Event()
        order: list[str] = []

        def first():
            with store.transaction("first") as tx:
                inside.set()
                time.sleep(0.3)
                tx.put_deployment(deployment(generation=1))
                tx.event("first", deployment_id=DEP)
                order.append("first-done")

        def second():
            inside.wait(5)
            with store.transaction("second") as tx:
                order.append("second-started")
                seen = tx.deployment(DEP).generation
                tx.put_deployment(deployment(generation=seen + 1))
                tx.event("second", deployment_id=DEP)

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert order == ["first-done", "second-started"]
        assert store.deployment(DEP).generation == 2
        assert [e.type for e in store.events()] == ["seeded", "first", "second"]

    # -- 5. write-once records -------------------------------------------------------------

    def test_release_is_written_once(self, store: Store):
        seed(store)
        with store.transaction("test") as tx:
            tx.add_release(release(recorded_at=T0 + timedelta(days=1)))  # same content
        assert store.release(REL1).recorded_at == T0
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_release(release(document={"spec": {"image": "other"}}))
        assert store.release(REL1) == release()

    def test_a_release_adopted_with_only_a_served_name_is_completed_once(self, store: Store):
        rid = release_id("model", "fast", "Qwen-fast")
        adopted = release(rid, digest="", origin="adopted", document={"servedName": "Qwen/fast"})
        full = adopted.model_copy(
            update={
                "digest": "sha256:" + "3" * 64,
                "document": {"servedName": "Qwen/fast", "engine": "fast"},
                "recorded_at": T0 + timedelta(days=1),
            }
        )
        with store.transaction("test") as tx:
            tx.put_item(Item(kind="model", name="fast", owner="p", source="s", description="d"))
            tx.add_release(adopted)
            tx.event("adopted")
        with store.transaction("test") as tx:
            tx.complete_release(full)
            tx.event("completed")
        assert store.release(rid) == full.model_copy(update={"recorded_at": T0})
        with store.transaction("test") as tx:
            tx.complete_release(full)  # the same content is not a change
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.complete_release(full.model_copy(update={"document": {"servedName": "Qwen/fast"}}))
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.complete_release(full.model_copy(update={"id": "model:fast:none", "name": "none"}))

    def test_release_needs_its_item(self, store: Store):
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.add_release(release())

    def test_deployment_needs_its_item(self, store: Store):
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.put_deployment(deployment())

    def test_requirements_and_provisions_are_written_once(self, store: Store):
        seed(store)
        requirements = [
            Requirement(
                release_id=REL1, type="tool", target="postgres-admin", names=["execute_sql"]
            ),
            Requirement(release_id=REL1, type="model", target="llm-main", names=["Some/Model"]),
        ]
        provisions = [Provision(release_id=REL1, type="capability", name="database-admin")]
        with store.transaction("test") as tx:
            tx.add_requirements(REL1, requirements)
            tx.add_provisions(REL1, provisions)
            tx.event("described")
        with store.transaction("test") as tx:  # same content: a no-op
            tx.add_requirements(REL1, requirements)
            tx.add_provisions(REL1, provisions)
        assert store.requirements(REL1) == requirements
        assert store.provisions(REL1) == provisions
        assert store.provisions() == provisions
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_requirements(REL1, requirements[:1])
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_provisions(REL1, [])
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.add_requirements(REL2, [])

    def test_requirements_can_be_searched_by_target(self, store: Store):
        seed(store)
        with store.transaction("test") as tx:
            tx.add_release(release(REL2))
            tx.add_requirements(
                REL1,
                [
                    Requirement(release_id=REL1, type="tool", target="postgres-admin"),
                    Requirement(release_id=REL1, type="model", target="llm-main"),
                ],
            )
            tx.add_requirements(
                REL2, [Requirement(release_id=REL2, type="tool", target="kubernetes")]
            )
            tx.event("described")
        assert len(store.requirements()) == 3
        assert [r.release_id for r in store.requirements(type="tool")] == [REL1, REL2]
        assert [r.release_id for r in store.requirements(type="tool", target="kubernetes")] == [
            REL2
        ]
        assert store.requirements(REL1, type="model")[0].target == "llm-main"
        assert store.requirements(type="tool", target="llm-main") == []

    def test_revision_numbers_are_in_sequence(self, store: Store):
        seed(store)
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_revision(revision(3))
        with store.transaction("test") as tx:
            tx.add_release(release(REL2))
            tx.add_revision(revision(2, REL2))
            tx.event("revision-created", deployment_id=DEP)
        assert [r.number for r in store.revisions(DEP)] == [1, 2]
        assert store.revisions("agent:kubani:tenant-test:unknown") == []

    def test_revision_is_written_once(self, store: Store):
        seed(store)
        with store.transaction("test") as tx:  # same content: a no-op
            tx.add_revision(revision(created_at=T0 + timedelta(hours=1)))
        assert store.revision(DEP, 1).created_at == T0
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_revision(revision(authority_digest="d" * 64))

    def test_revision_needs_its_deployment_and_release(self, store: Store):
        seed(store)
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.add_revision(revision(2, REL2))
        with pytest.raises(Missing), store.transaction("test") as tx:
            other = "agent:kubani:tenant-test:unknown"
            tx.add_revision(revision(id=revision_id(other, 1), deployment_id=other))

    def test_revision_progress_changes_only_what_is_given(self, store: Store):
        seed(store)
        with store.transaction("test") as tx:
            tx.add_approval(approval())
            got = tx.set_revision_progress(
                DEP, 1, phase="pending-approval", reason="waiting", generation=4
            )
            assert got.phase == "pending-approval"
            tx.event("progress", deployment_id=DEP)
        with store.transaction("test") as tx:
            tx.set_revision_progress(
                DEP, 1, phase="activated", approval_id="approval-1", activated_at=T0
            )
            tx.event("progress", deployment_id=DEP)
        got = store.revision(DEP, 1)
        assert (got.phase, got.approval_id, got.reason, got.activated_at, got.generation) == (
            "activated",
            "approval-1",
            "waiting",
            T0,
            4,
        )
        assert got.model_dump(
            exclude={"phase", "approval_id", "reason", "activated_at", "generation"}
        ) == revision().model_dump(
            exclude={"phase", "approval_id", "reason", "activated_at", "generation"}
        )
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.set_revision_progress(DEP, 9, phase="activated")
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.set_revision_progress(DEP, 1, approval_id="no-such-approval")

    def test_resolutions_are_written_once(self, store: Store):
        seed(store)
        rid = revision_id(DEP, 1)
        rows = [
            Resolution(
                revision_id=rid,
                type="tool",
                target="postgres-admin",
                resolved_deployment="tool-server:kubani:-:postgres-admin",
            ),
            Resolution(revision_id=rid, type="tool", target="unregistered"),
        ]
        with store.transaction("test") as tx:
            tx.add_resolutions(rid, rows)
            tx.event("resolved", deployment_id=DEP)
        with store.transaction("test") as tx:
            tx.add_resolutions(rid, rows)
        assert store.resolutions(rid) == rows
        assert store.resolutions(revision_id(DEP, 2)) == []
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_resolutions(rid, rows[:1])

    def test_approval_id_is_used_once(self, store: Store):
        seed(store)
        with store.transaction("test") as tx:
            tx.add_approval(approval())
            tx.event("approved", deployment_id=DEP)
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_approval(approval())  # the same record again is still a conflict
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.add_approval(approval("approval-2", number=5))
        assert store.approval("approval-1") == approval()
        assert store.approval("approval-2") is None

    def test_approval_state_and_order(self, store: Store):
        seed(store)
        with store.transaction("test") as tx:
            tx.add_approval(approval("b", approved_at=T0 + timedelta(minutes=1)))
            tx.add_approval(approval("z", approved_at=T0))
            tx.add_approval(approval("a", approved_at=T0 + timedelta(minutes=1)))
            tx.event("approved", deployment_id=DEP)
        assert [a.id for a in store.approvals(DEP)] == ["z", "a", "b"]
        assert [a.id for a in store.approvals()] == ["z", "a", "b"]
        assert store.approvals("agent:kubani:tenant-test:unknown") == []
        with store.transaction("test") as tx:
            tx.set_approval_state("z", "superseded")
            tx.event("superseded", deployment_id=DEP)
        assert store.approval("z").state == "superseded"
        assert store.approval("a").state == "active"
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.set_approval_state("nope", "superseded")

    def test_evaluation_is_added_and_read_back(self, store: Store):
        seed(store)
        with store.transaction("test") as tx:
            tx.add_evaluation(evaluation())
            tx.event("evaluation-requested", deployment_id=DEP)
        assert store.evaluation(evaluation().id) == evaluation()
        assert store.evaluations(DEP) == [evaluation()]
        assert store.evaluation("nope") is None

    def test_evaluation_attempts_count_up_from_one(self, store: Store):
        seed(store)
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_evaluation(evaluation(2))
        with store.transaction("test") as tx:
            tx.add_evaluation(evaluation(1))
            tx.event("evaluation-requested", deployment_id=DEP)
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_evaluation(evaluation(3))
        with store.transaction("test") as tx:
            tx.add_evaluation(evaluation(2))
            tx.event("evaluation-requested", deployment_id=DEP)
        assert [e.attempt for e in store.evaluations(DEP)] == [1, 2]

    def test_evaluation_is_written_once(self, store: Store):
        seed(store)
        with store.transaction("test") as tx:
            tx.add_evaluation(evaluation())
            tx.event("evaluation-requested", deployment_id=DEP)
        before = store.generation()
        with store.transaction("test") as tx:
            tx.add_evaluation(evaluation())
        assert store.generation() == before
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_evaluation(evaluation(suite_digest="f" * 64))
        assert store.evaluation(evaluation().id) == evaluation()

    def test_evaluation_progress_changes_only_what_is_given(self, store: Store):
        seed(store)
        with store.transaction("test") as tx:
            tx.add_evaluation(evaluation())
            tx.event("evaluation-requested", deployment_id=DEP)
        with store.transaction("test") as tx:
            got = tx.set_evaluation_progress(
                evaluation().id,
                state="claimed",
                claimed_by="evaluator-1",
                claim_expires_at=T0 + timedelta(minutes=5),
            )
            tx.event("evaluation-claimed", deployment_id=DEP)
        assert (got.state, got.claimed_by, got.claim_expires_at) == (
            "claimed",
            "evaluator-1",
            T0 + timedelta(minutes=5),
        )
        assert got.result == {} and got.finished_at is None
        assert store.evaluation(evaluation().id) == got
        with store.transaction("test") as tx:
            done = tx.set_evaluation_progress(
                evaluation().id,
                state="passed",
                evaluator_version="1.0",
                result={"verdict": "pass"},
                finished_at=T0 + timedelta(minutes=1),
            )
            tx.event("evaluation-finished", deployment_id=DEP)
        assert done.claimed_by == "evaluator-1"
        assert (done.state, done.evaluator_version, done.result) == (
            "passed",
            "1.0",
            {"verdict": "pass"},
        )
        assert done.model_dump(include={"id", "suite", "created_at"}) == evaluation().model_dump(
            include={"id", "suite", "created_at"}
        )
        assert store.evaluation(evaluation().id) == done
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.set_evaluation_progress("nope", state="error")

    def test_evaluations_filter_and_order(self, store: Store):
        seed(store)
        other = "agent:kubani:tenant-test:other"
        with store.transaction("test") as tx:
            tx.put_deployment(deployment(id=other))
            tx.add_revision(revision(id=revision_id(other, 1), deployment_id=other))
            tx.add_evaluation(evaluation(1, created_at=T0 + timedelta(minutes=1)))
            tx.add_evaluation(evaluation(2, created_at=T0))
            tx.add_evaluation(evaluation(1, other, created_at=T0))
            tx.set_evaluation_progress(evaluation(2).id, state="failed")
            tx.event("evaluation-requested", deployment_id=DEP)
        assert [e.id for e in store.evaluations()] == [
            evaluation(2).id,
            evaluation(1, other).id,
            evaluation(1).id,
        ]
        assert [e.id for e in store.evaluations(DEP)] == [evaluation(2).id, evaluation(1).id]
        assert [e.id for e in store.evaluations(state="failed")] == [evaluation(2).id]
        assert [e.id for e in store.evaluations(DEP, state="pending")] == [evaluation(1).id]
        assert store.evaluations(other, state="failed") == []

    def test_evaluation_needs_its_revision(self, store: Store):
        seed(store)
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.add_evaluation(evaluation(1, number=2))
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.add_evaluation(evaluation(1, "agent:kubani:tenant-test:unknown"))

    def test_revocation_is_written_once_apart_from_indexed_at(self, store: Store):
        with store.transaction("test") as tx:
            tx.add_revocation(revocation())
            tx.event("revoked")
        assert store.revocation("rev-1") == revocation()
        assert store.revocation("nope") is None
        before = store.generation()
        with store.transaction("test") as tx:
            tx.add_revocation(revocation(indexed_at=T0 + timedelta(hours=1)))
        assert store.generation() == before
        assert store.revocation("rev-1") == revocation()
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_revocation(revocation(reason="quality"))
        assert store.revocation("rev-1") == revocation()

    def test_revocations_filter_and_order(self, store: Store):
        with store.transaction("test") as tx:
            tx.add_revocation(revocation("b", revoked_at=T0 + timedelta(minutes=1)))
            tx.add_revocation(revocation("z", revoked_at=T0))
            tx.add_revocation(revocation("a", revoked_at=T0 + timedelta(minutes=1)))
            tx.add_revocation(revocation("m", kind="skill", item="lookup", revoked_at=T0))
            tx.event("revoked")
        assert [r.id for r in store.revocations()] == ["m", "z", "a", "b"]
        assert [r.id for r in store.revocations(kind="agent")] == ["z", "a", "b"]
        assert [r.id for r in store.revocations(item="lookup")] == ["m"]
        assert [r.id for r in store.revocations(kind="agent", item="db-admin")] == ["z", "a", "b"]
        assert store.revocations(kind="model") == []

    def test_assessment_is_written_once(self, store: Store):
        with store.transaction("test") as tx:
            tx.add_assessment(assessment())
            tx.event("assessment-created")
        assert store.assessment("asm-1") == assessment()
        assert store.assessment("nope") is None
        before = store.generation()
        with store.transaction("test") as tx:
            tx.add_assessment(assessment())
        assert store.generation() == before
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_assessment(assessment(blocked=True))
        assert store.assessment("asm-1") == assessment()

    def test_alias_move_is_added_and_needs_its_assessment(self, store: Store):
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.add_alias_move(move())
        with store.transaction("test") as tx:
            tx.add_assessment(assessment())
            tx.add_alias_move(move())
            tx.event("alias-move-started")
        assert store.alias_move("mv-1") == move()
        assert store.alias_move("nope") is None
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_alias_move(move(sessions="cancel"))

    def test_alias_move_progress_changes_only_what_is_given(self, store: Store):
        with store.transaction("test") as tx:
            tx.add_assessment(assessment())
            tx.add_alias_move(move())
            tx.event("alias-move-started")
        with store.transaction("test") as tx:
            got = tx.set_alias_move_progress("mv-1", state="held", held_at=T0 + timedelta(1))
            tx.event("alias-move-held")
        assert (got.state, got.held_at, got.moved_at, got.reason) == (
            "held",
            T0 + timedelta(1),
            None,
            "",
        )
        with store.transaction("test") as tx:
            done = tx.set_alias_move_progress("mv-1", state="cancelled", reason="changed my mind")
            tx.event("alias-move-cancelled")
        assert done.held_at == T0 + timedelta(1)
        assert store.alias_move("mv-1") == done
        assert done.model_dump(include={"id", "candidate", "started_at"}) == move().model_dump(
            include={"id", "candidate", "started_at"}
        )
        before = store.generation()
        with store.transaction("test") as tx:
            tx.set_alias_move_progress("mv-1", state="cancelled")
        assert store.generation() == before
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.set_alias_move_progress("nope", state="held")

    def test_an_alias_has_one_open_move(self, store: Store):
        with store.transaction("test") as tx:
            tx.add_assessment(assessment())
            tx.add_alias_move(move())
            tx.event("alias-move-started")
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_alias_move(move("mv-2"))
        with store.transaction("test") as tx:
            tx.add_alias_move(move("mv-3", alias="main"))
            tx.set_alias_move_progress("mv-1", state="completed")
            tx.event("alias-move-completed")
        with store.transaction("test") as tx:
            tx.add_alias_move(move("mv-2", started_at=T0 + timedelta(1)))
            tx.event("alias-move-started")
        assert [m.id for m in store.alias_moves(cluster="kubani", alias="fast", open=True)] == [
            "mv-2"
        ]

    def test_alias_moves_filter_and_order_newest_first(self, store: Store):
        with store.transaction("test") as tx:
            tx.add_assessment(assessment())
            tx.add_alias_move(move("a", started_at=T0, state="completed"))
            tx.add_alias_move(move("b", started_at=T0 + timedelta(1), state="cancelled"))
            tx.add_alias_move(move("c", started_at=T0 + timedelta(2), state="moving"))
            tx.add_alias_move(move("d", alias="main", started_at=T0 + timedelta(1)))
            tx.add_alias_move(move("e", cluster="other", started_at=T0 + timedelta(3)))
            tx.event("alias-move-started")
        assert [m.id for m in store.alias_moves()] == ["e", "c", "d", "b", "a"]
        assert [m.id for m in store.alias_moves(cluster="kubani")] == ["c", "d", "b", "a"]
        assert [m.id for m in store.alias_moves(cluster="kubani", alias="fast")] == ["c", "b", "a"]
        assert [m.id for m in store.alias_moves(cluster="kubani", open=True)] == ["c", "d"]
        assert [m.id for m in store.alias_moves(cluster="kubani", open=False)] == ["b", "a"]

    def test_hold_is_added_and_needs_its_move_and_deployment(self, store: Store):
        seed(store)
        with store.transaction("test") as tx:
            tx.add_assessment(assessment())
            tx.add_alias_move(move())
            tx.event("alias-move-started")
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.add_hold(hold("nope"))
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.add_hold(hold(dep="agent:kubani:tenant-test:unknown"))
        with store.transaction("test") as tx:
            tx.add_hold(hold())
            tx.event("alias-move-started")
        assert store.holds() == [hold()]
        assert store.holds(move_id="mv-1", deployment_id=DEP) == [hold()]
        assert store.holds(move_id="mv-2") == []
        assert store.holds(deployment_id="other") == []
        before = store.generation()
        with store.transaction("test") as tx:
            tx.add_hold(hold())
        assert store.generation() == before

    def test_hold_progress_changes_only_what_is_given(self, store: Store):
        seed(store)
        with store.transaction("test") as tx:
            tx.add_assessment(assessment())
            tx.add_alias_move(move())
            tx.add_hold(hold())
            tx.event("alias-move-started")
        with store.transaction("test") as tx:
            got = tx.set_hold_progress("mv-1", DEP, running=1)
            tx.event("hold-counted", deployment_id=DEP)
        assert (got.state, got.running, got.acknowledged_at) == ("holding", 1, None)
        with store.transaction("test") as tx:
            done = tx.set_hold_progress(
                "mv-1", DEP, state="held", running=0, acknowledged_at=T0 + timedelta(1)
            )
            tx.event("hold-held", deployment_id=DEP)
        assert (done.state, done.running, done.acknowledged_at) == ("held", 0, T0 + timedelta(1))
        assert store.holds() == [done]
        with pytest.raises(Missing), store.transaction("test") as tx:
            tx.set_hold_progress("mv-1", "nope", state="released")

    def test_a_deployment_has_one_hold_in_force(self, store: Store):
        seed(store)
        with store.transaction("test") as tx:
            tx.add_assessment(assessment())
            tx.add_alias_move(move("mv-1"))
            tx.add_alias_move(move("mv-2", alias="main"))
            tx.add_hold(hold("mv-1"))
            tx.event("alias-move-started")
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_hold(hold("mv-2"))
        with store.transaction("test") as tx:
            tx.set_hold_progress("mv-1", DEP, state="suspended")
            tx.event("hold-suspended", deployment_id=DEP)
        with pytest.raises(Conflict), store.transaction("test") as tx:
            tx.add_hold(hold("mv-2"))
        with store.transaction("test") as tx:
            tx.set_hold_progress("mv-1", DEP, state="resumed")
            tx.add_hold(hold("mv-2"))
            tx.event("hold-resumed", deployment_id=DEP)
        assert [(h.move_id, h.state) for h in store.holds(deployment_id=DEP)] == [
            ("mv-1", "resumed"),
            ("mv-2", "holding"),
        ]

    # -- 6. nothing is deleted -------------------------------------------------------------

    def test_there_is_no_delete(self, store: Store):
        with store.transaction("test") as tx:
            names = [n for n in dir(tx) if not n.startswith("_")]
        assert not [n for n in names if n.startswith(("delete", "remove", "drop"))]

    # -- 7. events and the generation ------------------------------------------------------

    def test_events_are_numbered_in_order(self, store: Store):
        seed(store)
        with store.transaction("someone") as tx:
            tx.put_deployment(deployment(enabled=False))
            first = tx.event("disabled", deployment_id=DEP, detail={"why": "test"})
            tx.put_deployment(deployment(enabled=True))
            second = tx.event("enabled", deployment_id=DEP)
        assert second.seq == first.seq + 1
        assert store.generation() == second.seq
        events = store.events()
        assert [e.seq for e in events] == sorted(e.seq for e in events)
        assert [e.type for e in events] == ["seeded", "disabled", "enabled"]
        assert events[1].actor == "someone"
        assert events[1].detail == {"why": "test"}
        assert events[1].at.tzinfo is not None
        assert [e.type for e in store.events(deployment_id=DEP)] == ["disabled", "enabled"]
        assert [e.type for e in store.events(after=first.seq)] == ["enabled"]
        assert [e.type for e in store.events(limit=2)] == ["seeded", "disabled"]

    def test_advance_generation(self, store: Store):
        seed(store)
        with store.transaction("test") as tx:
            tx.advance_generation(1000)
            event = tx.event("left-recovery")
        assert event.seq > 1000
        assert store.generation() == event.seq
        with store.transaction("test") as tx:
            tx.advance_generation(5)  # already past it: not a change
        assert store.generation() == event.seq

    def test_mode(self, store: Store):
        with store.transaction("test") as tx:
            tx.set_mode("recovery", "restored from a copy")
            tx.event("recovery-entered")
        meta = store.meta()
        assert (meta.mode, meta.mode_reason) == ("recovery", "restored from a copy")
        assert meta.mode_changed_at is not None
        with store.transaction("test") as tx:
            tx.set_mode("recovery", "restored from a copy")  # same: not a change

    # -- 8. observations -------------------------------------------------------------------

    def test_observations_need_no_transaction_and_no_event(self, store: Store):
        store.put_observations("kubani", [observation("r1"), observation("r2")])
        assert store.generation() == 0
        assert store.events() == []
        assert [o.name for o in store.observations("kubani")] == ["r1", "r2"]
        assert store.observation("kubani", "agentrelease", "tenant-test", "r1") == observation("r1")
        assert store.observation("kubani", "agent", "tenant-test", "r1") is None
        assert store.observations("elsewhere") == []

    def test_newer_observation_replaces_the_older(self, store: Store):
        store.put_observations("kubani", [observation("r1")])
        later = observation("r1", T0 + timedelta(seconds=30), document={"status": {"ready": True}})
        store.put_observations("kubani", [later])
        assert store.observations("kubani") == [later]

    def test_complete_snapshot_marks_missing_subjects_absent(self, store: Store):
        agent = observation("db-admin", subject_kind="agent")
        elsewhere = observation("r9", namespace="tenant-other")
        store.put_observations("kubani", [observation("r1"), observation("r2"), agent, elsewhere])
        later = T0 + timedelta(seconds=30)
        store.put_observations(
            "kubani",
            [observation("r1", later)],
            complete_for=[("agentrelease", "tenant-test")],
        )
        gone = store.observation("kubani", "agentrelease", "tenant-test", "r2")
        assert (gone.present, gone.observed_at) == (False, later)
        assert gone.document == observation("r2").document  # the last thing seen is kept
        assert store.observation("kubani", "agentrelease", "tenant-test", "r1").present is True
        # other kinds and namespaces were not part of the snapshot
        assert store.observation("kubani", "agent", "tenant-test", "db-admin") == agent
        assert store.observation("kubani", "agentrelease", "tenant-other", "r9") == elsewhere
        assert [
            o.name
            for o in store.observations(
                "kubani", subject_kind="agentrelease", namespace="tenant-test"
            )
        ] == ["r1", "r2"]

    def test_observation_of_another_cluster_is_refused(self, store: Store):
        with pytest.raises(Conflict):
            store.put_observations("kubani", [observation("r1", cluster="elsewhere")])
