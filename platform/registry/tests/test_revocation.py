"""Revocation (`contract/delivery.md` section 8): the feed, its effects, the report refusal and
what the cluster is told."""

from __future__ import annotations

import copy
import json
from datetime import timedelta

import pytest

from agent_registry import canonical, derive
from agent_registry.importer import apply_feed
from agent_registry.models import Revision, revision_id
from agent_registry.rules import recovery, revocation
from tests.store_suite import evaluation
from tests.test_report import REPORTS, body, reporting, send
from tests.world import CLUSTER, DEP, FIXTURES, NAMESPACE, T0, TENANT, World, registry_owned

FEED = FIXTURES / "feed.json"


def entry(rid: str = "2026-11-20-db-admin-aaaa", release: str | None = None, **changes) -> dict:
    values = {
        "schemaVersion": 1,
        "id": rid,
        "kind": "agent",
        "item": "db-admin",
        "release": release or "db-admin-2026-11-13-33c8acb2",
        "reason": "security",
        "summary": "leaks query text",
        "revokedBy": "al",
        "revokedAt": "2026-11-20T10:00:00Z",
    }
    return values | changes


def feed_with(*entries: dict) -> dict:
    feed = json.loads(FEED.read_text())
    feed["revocations"] = list(entries)
    return feed


def apply(world: World, *entries: dict, at=None) -> bool:
    return apply_feed(world.store, feed_with(*entries), now=at or world.now)


def release_name(world: World, number: int) -> str:
    return world.store.release(world.store.revision(DEP, number).release_id).name


def pin(world: World) -> None:
    with world.store.transaction("test") as tx:
        tx.event("pinned", deployment_id=DEP)
        tx.put_deployment(tx.deployment(DEP).model_copy(update={"pinned_revision": 2}))


def candidate(world: World, *, approved: bool = False) -> Revision:
    """A third revision of a new release, open, carrying a different Agent."""
    sent = reporting_body()
    agent = canonical.agent_document(sent["agent"])
    agent["spec"]["owner"] = "someone-new@example.com"
    release = derive.agent_release_record(sent["agentRelease"], recorded_at=world.now)
    authority = canonical.authority_of(agent)
    with world.store.transaction("test") as tx:
        tx.add_release(release)
        tx.add_revision(
            Revision(
                id=revision_id(DEP, 3),
                deployment_id=DEP,
                number=3,
                release_id=release.id,
                origin="registry",
                agent_document=agent,
                authority=authority,
                authority_digest=canonical.authority_digest(authority),
                created_at=world.now,
                phase="evaluating",
                approval_id="imported:" + DEP + "@2" if approved else None,
            )
        )
        tx.add_evaluation(evaluation(1, DEP, 3, created_at=world.now))
        tx.event("test-candidate", deployment_id=DEP)
    return world.store.revision(DEP, 3)


def reporting_body() -> dict:
    from tests.world import published

    return published("a")


# -- the feed ----------------------------------------------------------------------------------


def test_a_new_revocation_is_stored_once_and_stored_again_it_changes_nothing():
    world = registry_owned()
    before = world.store.generation()

    assert apply(world, entry()) is True
    stored = world.store.revocation("2026-11-20-db-admin-aaaa")
    after = world.store.generation()
    assert apply(world, entry(), at=T0 + timedelta(hours=1)) is False

    assert stored is not None
    assert (stored.reason, stored.sessions, stored.indexed_at) == ("security", "cancel", T0)
    assert world.store.generation() == after > before
    assert world.store.revocation(stored.id).indexed_at == T0
    types = [e.type for e in world.store.events(limit=1000)]
    assert types.count("release-revoked") == 1


def test_quality_defaults_to_finish():
    world = registry_owned()
    apply(world, entry(reason="quality"))
    assert world.store.revocation("2026-11-20-db-admin-aaaa").sessions == "finish"


def test_a_stored_revocation_stated_differently_refuses_the_whole_feed():
    world = registry_owned()
    apply(world, entry())
    changed = feed_with(entry(summary="something else"), entry("2026-11-21-db-admin-bbbb"))
    changed["tenants"][0]["namespace"] = "elsewhere"
    generation = world.store.generation()

    with pytest.raises(ValueError, match="stored differently"):
        apply_feed(world.store, changed, now=world.now)

    assert world.store.generation() == generation
    assert world.store.revocation("2026-11-21-db-admin-bbbb") is None
    assert world.store.tenant(CLUSTER, TENANT).namespace == NAMESPACE


def test_a_revocation_missing_from_a_later_feed_stays():
    world = registry_owned()
    apply(world, entry())

    assert apply(world) is False

    assert world.store.revocation("2026-11-20-db-admin-aaaa") is not None


@pytest.mark.parametrize(
    "bad",
    [
        {"reason": "boredom"},
        {"sessions": "kill"},
        {"kind": "widget"},
        {"revokedAt": "yesterday"},
        {"surprise": 1},
    ],
)
def test_a_badly_formed_revocation_refuses_the_feed(bad):
    with pytest.raises(ValueError):
        apply_feed(registry_owned().store, feed_with(entry(**bad)))


def test_revocations_must_be_unique_and_in_id_order():
    world = registry_owned()
    with pytest.raises(ValueError, match="id order"):
        apply(world, entry("b"), entry("a"))
    with pytest.raises(ValueError, match="unique"):
        apply(world, entry("a"), entry("a"))


# -- the effects of 8.2 ------------------------------------------------------------------------


def test_every_revision_of_the_release_is_written_again():
    world = registry_owned()
    before = {r.number: r.generation for r in world.store.revisions(DEP)}

    apply(world, entry())

    after = {r.number: r.generation for r in world.store.revisions(DEP)}
    hit = [n for n in after if release_name(world, n) == entry()["release"]]
    assert hit and all(after[n] > before[n] for n in hit)
    assert all(after[n] == before[n] for n in after if n not in hit)


def test_an_open_revision_of_the_release_is_refused_and_its_evaluations_cancelled():
    world = registry_owned()
    new = candidate(world, approved=True)
    with world.store.transaction("test") as tx:
        seq = tx.event("candidate-in-force", deployment_id=DEP).seq
        deployment = tx.deployment(DEP)
        tx.put_deployment(
            deployment.model_copy(
                update={
                    "generation": seq,
                    "document": {"agent": new.agent_document},
                }
            )
        )
    release = world.store.release(new.release_id).name

    apply(world, entry(release=release))

    revision = world.store.revision(DEP, 3)
    assert revision.phase == "refused"
    assert revision.reason == "release revoked: 2026-11-20-db-admin-aaaa"
    assert revision.generation > new.generation
    assert world.store.evaluation(f"{DEP}@3#1").state == "cancelled"
    deployment = world.store.deployment(DEP)
    # The Agent in force is settled again: back to the current revision's.
    assert deployment.document["agent"] == world.store.revision(DEP, 2).agent_document
    assert deployment.current_revision == 2


def test_a_current_revision_clears_the_pointer_and_the_pin():
    world = registry_owned()
    pin(world)
    before = world.store.deployment(DEP).generation

    apply(world, entry(release=release_name(world, 2)))

    deployment = world.store.deployment(DEP)
    assert (deployment.current_revision, deployment.pinned_revision) == (None, None)
    assert deployment.generation > before
    events = [e for e in world.store.events(deployment_id=DEP) if e.type == "current-cleared"]
    assert len(events) == 1
    assert world.store.revision(DEP, 2).phase == "activated"


def test_an_activated_revision_that_is_not_current_stays_activated():
    world = registry_owned()

    apply(world, entry(release=release_name(world, 1)))

    assert world.store.revision(DEP, 1).phase == "activated"
    assert world.store.deployment(DEP).current_revision == 2
    assert not [e for e in world.store.events(deployment_id=DEP) if e.type == "current-cleared"]


def test_a_pin_on_another_revision_is_kept():
    world = registry_owned()
    pin(world)

    apply(world, entry(release=release_name(world, 1)))

    assert world.store.deployment(DEP).pinned_revision == 2


def test_a_revocation_of_a_release_on_the_pull_request_path_changes_nothing_else():
    world = World()
    world.seed()
    snapshot = lambda: (  # noqa: E731
        world.store.deployment(DEP),
        world.store.revisions(DEP),
        world.store.evaluations(DEP),
    )
    before = snapshot()

    apply(world, entry())

    assert world.store.revocation("2026-11-20-db-admin-aaaa") is not None
    assert snapshot() == before


# -- the report --------------------------------------------------------------------------------


def test_a_report_of_a_revoked_release_is_refused_and_records_nothing():
    world = reporting()
    sent = body("a")
    apply(world, entry(release=sent["agentRelease"]["metadata"]["name"]))
    before = (world.store.generation(), len(world.store.revisions(DEP)))

    answer = send(world, sent)

    assert answer.status_code == 409
    assert answer.json()["error"]["code"] == "refused"
    assert "2026-11-20-db-admin-aaaa" in answer.json()["error"]["message"]
    assert (world.store.generation(), len(world.store.revisions(DEP))) == before
    assert REPORTS


# -- what the cluster is told ------------------------------------------------------------------


def desired(world: World) -> dict:
    answer = world.get(f"/v1/clusters/{CLUSTER}/desired", token="provisioner")
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_desired_lists_a_fresh_security_revocation_that_cancels_sessions():
    world = registry_owned()
    mine = release_name(world, 2)
    other = release_name(world, 1)
    apply(
        world,
        entry("a-security", release=mine),
        entry("b-quality", release=other, reason="quality"),
        entry("c-finish", release=other, sessions="finish"),
    )
    world.tick(3600)

    assert desired(world)["revocations"] == [
        {
            "id": "a-security",
            "tenant": TENANT,
            "namespace": NAMESPACE,
            "agent": "db-admin",
            "release": mine,
            "sessions": "cancel",
        }
    ]


def test_desired_leaves_out_a_revocation_older_than_the_setting(monkeypatch):
    world = registry_owned()
    apply(world, entry("a-old", release=release_name(world, 2)))
    monkeypatch.setenv("REGISTRY_REVOCATION_CANCEL_SECONDS", "60")
    world.tick(30)
    assert [r["id"] for r in desired(world)["revocations"]] == ["a-old"]

    world.tick(31)

    assert desired(world)["revocations"] == []
    assert revocation.cancel_seconds({}) == 86400


# -- recovery ----------------------------------------------------------------------------------


def test_a_database_restored_from_before_the_revocation_does_not_have_the_release_current():
    world = registry_owned()  # the state before: release of revision 2 is current
    assert world.store.deployment(DEP).current_revision == 2
    current = release_name(world, 2)

    apply(world, entry(release=current))  # the registry starts and applies the feed

    deployment = world.store.deployment(DEP)
    assert deployment.current_revision is None
    # The cluster still names that release as current; recovery must not adopt it back.
    world.cluster_shows_agent()
    observed = copy.deepcopy(world.store.observation(CLUSTER, "agent", NAMESPACE, "db-admin"))
    document = observed.document
    document["metadata"]["annotations"]["registry.agentplatform.io/current-release"] = current
    world.store.put_observations(CLUSTER, [observed.model_copy(update={"document": document})])
    found = [
        d for d in recovery.differences_of(world.store, deployment) if d.field == "current-release"
    ]
    assert found
    with world.store.transaction("test") as tx:
        recovery._adopt(tx, tx.deployment(DEP), found, tx.event("x").seq, world.now)
    assert world.store.deployment(DEP).current_revision is None
