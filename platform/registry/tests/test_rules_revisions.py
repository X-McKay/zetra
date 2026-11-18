"""The three functions the lifecycle rules and the revocation rules share
(`contract/delivery.md` sections 4, 5 and 8)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent_registry.rules import revisions, revocation
from agent_registry.store.memory import MemoryStore
from tests.store_suite import (
    DEP,
    REL1,
    REL2,
    T0,
    cluster,
    deployment,
    evaluation,
    item,
    release,
    revision,
    tenant,
)
from tests.store_suite import revocation as revocation_record

OLD = {"spec": {"owner": "old@example.com"}}
NEW = {"spec": {"owner": "new@example.com"}}
STORED = {"spec": {"owner": "stored@example.com"}}


def world(*revs, current: int | None = None, authority: str = "registry") -> MemoryStore:
    """A deployment whose stored Agent is STORED, with the revisions given."""
    store = MemoryStore()
    with store.transaction("test") as tx:
        tx.put_cluster(cluster())
        tx.put_tenant(tenant())
        tx.put_item(item())
        tx.put_deployment(
            deployment(authority=authority, current_revision=current, document={"agent": STORED})
        )
        tx.add_release(release(REL1))
        tx.add_release(release(REL2))
        for rev in revs:
            tx.add_revision(rev)
        tx.event("seeded")
    return store


def settle(store: MemoryStore, seq: int | None = None) -> bool:
    with store.transaction("test") as tx:
        if seq is None:
            return revisions.settle_agent(tx, DEP, None)
        return revisions.settle_agent(tx, DEP, tx.event("something").seq)


def agent(store: MemoryStore) -> dict:
    return store.deployment(DEP).document["agent"]


# -- the Agent in force ----------------------------------------------------------------------


def test_an_approved_candidate_is_in_force():
    store = world(
        revision(1, REL1, agent_document=OLD, phase="activated"),
        revision(2, REL2, agent_document=NEW, phase="awaiting-runtime", approval_id="a1"),
        current=1,
    )
    assert settle(store) is True
    assert agent(store) == NEW


@pytest.mark.parametrize("phase", ["awaiting-runtime", "pending-approval"])
def test_an_unapproved_candidate_changes_nothing_that_runs(phase):
    store = world(
        revision(1, REL1, agent_document=OLD, phase="activated"),
        revision(2, REL2, agent_document=NEW, phase=phase),
        current=1,
    )
    settle(store)
    assert agent(store) == OLD


@pytest.mark.parametrize("phase", ["refused", "superseded"])
def test_after_a_candidate_ends_the_current_revision_is_in_force(phase):
    store = world(
        revision(1, REL1, agent_document=OLD, phase="activated"),
        revision(2, REL2, agent_document=NEW, phase=phase, approval_id="a1"),
        current=1,
    )
    settle(store)
    assert agent(store) == OLD


def test_a_pin_keeps_the_current_revision_in_force_over_a_newer_activated_one():
    store = world(
        revision(1, REL1, agent_document=OLD, phase="activated"),
        revision(2, REL2, agent_document=NEW, phase="activated", approval_id="a1"),
        current=1,
    )
    settle(store)
    assert agent(store) == OLD


def test_with_nothing_current_the_newest_revision_is_in_force():
    store = world(revision(1, REL1, agent_document=NEW, phase="pending-approval"))
    settle(store)
    assert agent(store) == NEW


def test_with_nothing_current_after_a_revocation_the_agent_stays():
    store = world(
        revision(1, REL1, agent_document=OLD, phase="activated"),
        revision(2, REL2, agent_document=NEW, phase="refused"),
    )
    with store.transaction("test") as tx:
        tx.add_revocation(revocation_record(release=release(REL1).name))
        tx.event("release-revoked")
    assert settle(store) is False
    assert agent(store) == STORED


def test_settling_sets_the_generation_to_the_transactions_event():
    store = world(revision(1, REL1, agent_document=NEW, phase="activated"), current=1)
    with store.transaction("test") as tx:
        seq = tx.event("something").seq
        assert revisions.settle_agent(tx, DEP, seq) is True
    assert store.deployment(DEP).generation == seq
    assert [e.type for e in store.events()][-1] == "something"


def test_settling_alone_writes_its_own_event_and_only_on_a_change():
    store = world(revision(1, REL1, agent_document=NEW, phase="activated"), current=1)
    assert settle(store) is True
    last = store.events()[-1]
    assert last.type == "agent-settled"
    assert store.deployment(DEP).generation == last.seq
    count = len(store.events())
    assert settle(store) is False
    assert len(store.events()) == count


def test_a_deployment_on_the_pull_request_path_is_left_alone():
    store = world(
        revision(1, REL1, agent_document=NEW, phase="activated"),
        current=1,
        authority="pull-request",
    )
    assert settle(store) is False
    assert agent(store) == STORED


# -- open evaluations ------------------------------------------------------------------------


def test_cancel_open_evaluations_cancels_pending_and_claimed_of_that_revision_only():
    store = world(
        revision(1, REL1, phase="activated"),
        revision(2, REL2, phase="evaluating", approval_id="a1"),
    )
    later = T0 + timedelta(minutes=5)
    with store.transaction("test") as tx:
        tx.add_evaluation(evaluation(1, number=1, state="passed"))
        tx.add_evaluation(evaluation(1, number=2, state="error"))
        tx.add_evaluation(evaluation(2, number=2, state="claimed", claimed_by="e1"))
        tx.event("evaluation-requested")
    with store.transaction("test") as tx:
        ids = revisions.cancel_open_evaluations(tx, DEP, 2, now=later)
        tx.event("revision-refused")
    states = {e.id: (e.state, e.finished_at) for e in store.evaluations(DEP)}
    assert ids == [evaluation(2, number=2).id]
    assert states[evaluation(2, number=2).id] == ("cancelled", later)
    assert states[evaluation(1, number=2).id][0] == "error"
    assert states[evaluation(1, number=1).id][0] == "passed"
    with store.transaction("test") as tx:
        assert revisions.cancel_open_evaluations(tx, DEP, 2, now=later) == []


# -- revoked ---------------------------------------------------------------------------------


def test_revoked_finds_the_revocation_of_a_release_by_kind_item_and_name():
    store = world(revision(1, REL1, phase="activated"))
    one, two = release(REL1), release(REL2)
    assert revocation.revoked(store, one) is None
    with store.transaction("test") as tx:
        tx.add_revocation(revocation_record("r-1", release=one.name))
        tx.add_revocation(revocation_record("r-2", kind="skill", release=two.name))
        tx.add_revocation(revocation_record("r-3", item="other", release=two.name))
        tx.event("release-revoked")
    assert revocation.revoked(store, one).id == "r-1"
    assert revocation.revoked(store, two) is None
