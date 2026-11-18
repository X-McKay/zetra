"""What `/v1/status` says about an agent: `contract/lifecycle.md` section 12, row by row."""

from __future__ import annotations

import pytest

from agent_registry import canonical, derive, modes
from agent_registry.models import Observation, Revision, revision_id
from tests.world import AGENT, CLUSTER, DEP, NAMESPACE, World, published, registry_owned

OLD = 181  # seconds; the limit is 180


def agent_status(world: World) -> dict:
    body = world.get("/v1/status", cluster=CLUSTER).json()
    assert [a["deployment"] for a in body["agents"]] == [DEP]
    return body["agents"][0]


def release_name(world: World, number: int) -> str:
    return world.store.release(world.store.revision(DEP, number).release_id).name


def add_revision(world: World, letter: str, *, phase: str, reason: str = "", approved=False) -> str:
    """Revision 3, as a release recorded after the two imported ones."""
    body = published(letter)
    release = derive.agent_release_record(body["agentRelease"], recorded_at=world.now)
    agent = body["agent"]
    authority = canonical.authority_of(agent)
    with world.store.transaction("test") as tx:
        tx.add_release(release)
        tx.add_requirements(release.id, derive.requirements_of(release.id, agent))
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
                phase=phase,
                reason=reason,
                approval_id=f"imported:{DEP}@2" if approved else None,
            )
        )
        tx.event("release-recorded", deployment_id=DEP)
    return release.name


def edit_deployment(world: World, **changes) -> None:
    with world.store.transaction("test") as tx:
        tx.put_deployment(tx.deployment(DEP).model_copy(update=changes))
        tx.event("test", deployment_id=DEP)


def project(world: World, kind: str, name: str, result: str, message: str = "") -> None:
    world.store.put_observations(
        CLUSTER,
        [
            Observation(
                cluster=CLUSTER,
                subject_kind="projection",
                namespace=NAMESPACE,
                name=f"{kind}/{name}",
                observed_at=world.now,
                document={"deployment": DEP, "generation": 3, "result": result, "message": message},
            )
        ],
    )


def test_an_agent_with_nothing_to_say():
    found = agent_status(registry_owned())

    assert (found["blockers"], found["nextActions"]) == ([], [])
    assert found["ready"] is True
    assert (found["currentRevision"], found["newestRevision"], found["phase"]) == (
        2,
        2,
        "activated",
    )
    assert found["observation"]["state"] == "fresh"


def test_recovery():
    world = registry_owned()
    modes.enter_recovery(world.store, "a test", actor="test")

    found = agent_status(world)

    assert found["blockers"] == ["the registry is in recovery; nothing is being changed"]
    assert world.get("/v1/status", cluster=CLUSTER).json()["mode"] == "recovery"


def test_recovery_says_nothing_about_an_agent_the_pull_request_path_owns():
    world = World()
    world.seed()
    world.cluster_shows_everything()
    modes.enter_recovery(world.store, "a test", actor="test")

    assert agent_status(world)["blockers"] == []


def test_an_agent_never_reported():
    world = World()
    world.seed()

    found = agent_status(world)

    assert found["blockers"] == ["the cluster has never reported this agent"]
    assert (found["ready"], found["observation"]) == (
        None,
        {"state": "missing", "observedAt": None, "ageSeconds": None},
    )


def test_an_agent_last_reported_long_ago():
    world = registry_owned()
    world.tick(OLD)

    found = agent_status(world)

    assert found["blockers"] == [f"the cluster last reported this agent {OLD} s ago"]
    assert found["ready"] is None  # nothing fresh to read it from
    assert found["observation"]["state"] == "old"


def test_an_agent_the_cluster_says_is_not_there():
    world = registry_owned()
    shown = world.cluster_shows_agent()
    world.store.put_observations(CLUSTER, [shown.model_copy(update={"present": False})])

    assert agent_status(world)["blockers"] == ["the agent is not on the cluster"]


def test_a_disabled_agent():
    world = registry_owned()
    edit_deployment(world, enabled=False)

    assert agent_status(world)["blockers"] == ["the agent is disabled"]


@pytest.mark.parametrize(
    ("result", "message", "blockers", "actions"),
    [
        (
            "refused-flux-inventory",
            "",
            ["Flux still owns Agent/db-admin"],
            [f"merge the pull request that removes {AGENT} from the Flux kustomization"],
        ),
        ("refused-older", "", ["the cluster holds a newer Agent/db-admin than the registry"], []),
        ("error", "forbidden", ["Agent/db-admin could not be written: forbidden"], []),
        ("applied", "", [], []),
    ],
)
def test_what_the_provisioner_could_not_write(result, message, blockers, actions):
    world = registry_owned()
    project(world, "agent", AGENT, result, message)

    found = agent_status(world)

    assert (found["blockers"], found["nextActions"]) == (blockers, actions)


def test_a_release_object_the_provisioner_could_not_write_is_named_by_kind_and_name():
    world = registry_owned()
    name = release_name(world, 2)
    project(world, "agentrelease", name, "refused-older")

    assert agent_status(world)["blockers"] == [
        f"the cluster holds a newer AgentRelease/{name} than the registry"
    ]


def test_an_old_or_unreadable_projection_result_is_ignored():
    world = registry_owned()
    project(world, "agent", AGENT, "error", "forbidden")
    world.tick(OLD)
    world.cluster_shows_everything()  # everything else is fresh again
    assert agent_status(world)["blockers"] == []

    world.store.put_observations(
        CLUSTER,
        [
            Observation(
                cluster=CLUSTER,
                subject_kind="projection",
                namespace=NAMESPACE,
                name=f"agent/{AGENT}",
                observed_at=world.now,
                document={"result": ["not", "a", "result"]},
            )
        ],
    )
    assert agent_status(world)["blockers"] == []


def test_a_new_release_nobody_has_approved_on_the_pull_request_path():
    world = World()
    world.seed()
    world.cluster_shows_agent()
    name = add_revision(world, "c", phase="awaiting-runtime", reason="waiting for the verdict")

    found = agent_status(world)

    assert found["blockers"] == [f"release {name}: waiting for the verdict"]
    assert found["nextActions"] == [f"just agent approve {AGENT} {name}"]
    assert (found["newestRevision"], found["newestRelease"]) == (3, name)
    assert (found["currentRevision"], found["phase"]) == (2, "awaiting-runtime")


def test_a_new_release_nobody_has_approved_under_the_registry_has_no_command_to_run():
    world = registry_owned()
    name = add_revision(world, "c", phase="awaiting-runtime", reason="waiting for the verdict")

    found = agent_status(world)

    assert found["blockers"] == [f"release {name}: waiting for the verdict"]
    assert found["nextActions"] == []


def test_a_release_waiting_for_approval():
    world = registry_owned()
    name = add_revision(world, "c", phase="pending-approval", reason="adds a tool")

    found = agent_status(world)

    assert found["blockers"] == [f"release {name} waits for approval: adds a tool"]
    assert found["nextActions"] == [f"just agent approve {AGENT}"]


def test_an_approved_release_waiting_for_ready():
    world = registry_owned()
    name = add_revision(
        world, "c", phase="awaiting-runtime", reason="approved; waiting for Ready", approved=True
    )

    found = agent_status(world)

    assert found["blockers"] == [f"release {name}: approved; waiting for Ready"]
    assert found["nextActions"] == []


def test_a_pinned_agent_with_a_later_revision_activated():
    world = registry_owned()
    edit_deployment(world, pinned_revision=1, current_revision=1)
    world.cluster_shows_everything()

    found = agent_status(world)

    assert found["blockers"] == ["pinned to revision 1; revision 2 is activated and not in use"]
    assert (found["pinnedRevision"], found["currentRevision"]) == (1, 1)
    assert found["currentRelease"] == release_name(world, 1)


def test_a_current_release_that_is_not_ready():
    world = registry_owned()
    world.cluster_shows(2, ready=False)

    found = agent_status(world)

    assert found["blockers"] == [
        f"current release {release_name(world, 2)} is not Ready: NotApproved"
    ]
    assert found["ready"] is False


def test_blockers_follow_the_order_of_the_table():
    world = registry_owned()
    modes.enter_recovery(world.store, "a test", actor="test")
    edit_deployment(world, enabled=False)
    project(world, "agent", AGENT, "refused-older")
    name = add_revision(world, "c", phase="pending-approval", reason="adds a tool")
    world.cluster_shows(2, ready=False)

    assert agent_status(world)["blockers"] == [
        "the registry is in recovery; nothing is being changed",
        "the agent is disabled",
        "the cluster holds a newer Agent/db-admin than the registry",
        f"release {name} waits for approval: adds a tool",
        f"current release {release_name(world, 2)} is not Ready: NotApproved",
    ]
