"""Tool contracts and model releases in the feed: `contract/dependencies.md` sections 2 and 4."""

from __future__ import annotations

import copy
import json

import pytest

from agent_registry import canonical, derive
from agent_registry.importer import apply_feed
from agent_registry.models import release_id, release_slug
from agent_registry.store.memory import MemoryStore
from tests.test_importer import everything
from tests.world import CLUSTER, DEP, FIXTURES, T0, World, registry_owned

SERVER = "postgres-admin-mcp"
SERVED = "Qwen3.6-35B-A3B-NVFP4"  # engine `main`, which db-admin's model backend reaches
CONFIG = "sha256:" + "c" * 64


def feed() -> dict:
    return json.loads((FIXTURES / "feed.json").read_text())


def entry(f: dict, section: str, name: str) -> dict:
    return next(e for e in f[section] if e["name"] == name)


def tool(name: str, *, properties: dict | None = None, description: str = "a tool") -> dict:
    return {
        "name": name,
        "description": description,
        "effect": "remote_read",
        "inputSchema": {"type": "object", "properties": properties or {}, "required": []},
        "outputSchema": None,
    }


def with_contract(f: dict, tools: list[dict], config: str = CONFIG) -> dict:
    """The feed with a contract for postgres-admin-mcp, as the cluster repository renders it."""
    f = copy.deepcopy(f)
    server = entry(f, "toolServers", SERVER)
    server["configDigest"] = config
    server["contract"] = {
        "schemaVersion": 1,
        "server": server["name"],
        "backend": server["backend"],
        "image": server["image"],
        "configDigest": config,
        "tools": tools,
    }
    return f


def manifest(name: str, *, engine: str = "main", served: str = SERVED, **changes) -> dict:
    return {
        "schemaVersion": 1,
        "engine": engine,
        "name": name,
        "servedName": served,
        "weights": {"source": f"hf://{served}", "revision": None, "files": None},
        "format": "safetensors",
        "quantisation": None,
        "engineImage": "vllm/vllm-openai:v0.30.0-aarch64",
        "serving": {"chatTemplate": None, "toolCallParser": None, "contextLength": None},
        "offers": {"contextLength": None, "toolCalling": True},
        "evidence": {"benchmark": "a.json", "changeRecord": "b.md"},
    } | changes


def with_release(f: dict, manifests: list[dict], serves: str | None, engine: str = "main") -> dict:
    f = copy.deepcopy(f)
    f["modelReleases"] = manifests
    if serves is not None:
        entry(f, "modelEngines", engine)["release"] = serves
    return f


def owned(f: dict | None = None) -> World:
    """db-admin registry-owned, with the servers and engines of the feed in the store."""
    world = registry_owned()
    apply_feed(world.store, f or feed(), now=T0)
    return world


def newest(world: World) -> int:
    return world.store.revisions(DEP)[-1].number


def resolution(world: World, target: str):
    revision = world.store.revisions(DEP)[-1]
    return next(r for r in world.store.resolutions(revision.id) if r.target == target)


# ---- 1. a contract is stored ------------------------------------------------------------


def test_a_contract_is_stored_on_the_tool_servers_deployment_with_its_digests():
    store = MemoryStore(clock=lambda: T0)
    f = with_contract(feed(), [tool("execute_sql"), tool("explain")])

    assert apply_feed(store, f, now=T0)

    document = store.deployment(f"tool-server:{CLUSTER}:-:{SERVER}").document
    assert document["configDigest"] == CONFIG
    contract = document["contract"]
    fields = {k: v for k, v in tool("execute_sql").items() if k != "name"}
    assert contract["tools"]["execute_sql"] == fields | {
        "digest": "sha256:" + canonical.digest(fields)
    }
    digests = {name: t["digest"] for name, t in contract["tools"].items()}
    assert contract["digest"] == "sha256:" + canonical.digest(digests)
    assert contract["image"] == entry(f, "toolServers", SERVER)["image"]
    assert contract["configDigest"] == CONFIG
    assert not apply_feed(store, f, now=T0)


# ---- 2. what a feed may not say ----------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.update(server="other"),
        lambda c: c.update(backend="other"),
        lambda c: c.update(configDigest="sha256:" + "d" * 64),
        lambda c: c.update(image="docker.io/crystaldba/postgres-mcp@sha256:" + "e" * 64),
    ],
    ids=["server", "backend", "configDigest", "image-digest"],
)
def test_a_contract_that_disagrees_with_its_entry_refuses_the_feed_and_writes_nothing(change):
    store = MemoryStore(clock=lambda: T0)
    apply_feed(store, feed(), now=T0)
    before, generation = everything(store), store.generation()
    bad = with_contract(feed(), [tool("execute_sql")])
    change(entry(bad, "toolServers", SERVER)["contract"])

    with pytest.raises(ValueError):
        apply_feed(store, bad, now=T0)

    assert everything(store) == before and store.generation() == generation


def test_a_server_with_no_contract_is_valid():
    f = with_contract(feed(), [tool("execute_sql")])
    entry(f, "toolServers", SERVER)["contract"] = None
    store = MemoryStore(clock=lambda: T0)

    apply_feed(store, f, now=T0)

    assert store.deployment(f"tool-server:{CLUSTER}:-:{SERVER}").document["contract"] is None


# ---- 3. an older feed reads as before ----------------------------------------------------


def test_a_feed_without_the_new_keys_resolves_as_before_and_records_no_revision():
    world = owned()
    number = newest(world)

    assert not apply_feed(world.store, feed(), now=T0)

    assert newest(world) == number
    assert resolution(world, "postgres-admin").detail.keys() == {"image"}
    model = resolution(world, "llm-main")
    assert model.detail == {"alias": "main", "servedName": SERVED}
    assert model.resolved_release is None


# ---- 4. a changed contract is a new revision, an unrelated one is not --------------------


def test_a_changed_contract_of_a_required_tool_records_a_revision_and_another_tool_does_not():
    base = with_contract(feed(), [tool("execute_sql"), tool("explain")])
    world = owned(base)
    number = newest(world)
    assert "contract" in resolution(world, "postgres-admin").detail

    other = with_contract(feed(), [tool("execute_sql"), tool("explain", description="changed")])
    apply_feed(world.store, other, now=T0)
    assert newest(world) == number

    wider = with_contract(
        feed(), [tool("execute_sql", properties={"sql": {"type": "string"}}), tool("explain")]
    )
    assert apply_feed(world.store, wider, now=T0)
    assert newest(world) == number + 1
    revision = world.store.revisions(DEP)[-1]
    assert revision.origin == "registry"


def test_a_required_tool_the_contract_does_not_list_is_null_in_the_digest():
    world = owned(with_contract(feed(), [tool("explain")]))

    detail = resolution(world, "postgres-admin").detail

    assert detail["contract"] == derive.sha({"execute_sql": None})


# ---- 5. model releases -------------------------------------------------------------------


def test_a_manifest_is_a_model_release_whose_digest_ignores_evidence():
    store = MemoryStore(clock=lambda: T0)
    first = manifest("Main-r2")
    f = with_release(feed(), [first], "Main-r2")

    apply_feed(store, f, now=T0)

    release = store.release(release_id("model", "main", "Main-r2"))
    assert (release.kind, release.item, release.origin) == ("model", "main", "adopted")
    assert release.document == first
    assert release.digest == "sha256:" + canonical.digest(
        {k: v for k, v in first.items() if k != "evidence"}
    )
    assert store.deployment(f"model:{CLUSTER}:-:main").document["release"] == "Main-r2"
    moved = copy.deepcopy(first)
    moved["evidence"] = {"benchmark": "other.json", "changeRecord": "other.md"}
    assert derive.sha({k: v for k, v in moved.items() if k != "evidence"}) == release.digest


def test_a_release_adopted_with_only_a_served_name_is_completed_by_its_manifest():
    store = MemoryStore(clock=lambda: T0)
    apply_feed(store, feed(), now=T0)
    adopted = release_id("model", "main", release_slug(SERVED))
    assert store.release(adopted).document == {"servedName": SERVED}
    full = manifest(release_slug(SERVED))

    apply_feed(store, with_release(feed(), [full], release_slug(SERVED)), now=T0)

    assert store.release(adopted).document == full
    assert store.release(adopted).origin == "adopted"
    assert store.release(adopted).recorded_at == T0

    again = with_release(feed(), [full], release_slug(SERVED))
    assert not apply_feed(store, again, now=T0)

    different = with_release(feed(), [manifest(release_slug(SERVED), format="gguf")], None)
    before = everything(store)
    with pytest.raises(ValueError, match="never edited"):
        apply_feed(store, different, now=T0)
    assert everything(store) == before


# ---- 6. an engine's release must be a manifest of that engine and served name ------------


@pytest.mark.parametrize(
    ("build", "match"),
    [
        (lambda f: with_release(f, [], "ghost"), "not in modelReleases or stored"),
        (
            lambda f: with_release(
                f, [manifest("Fast-r2", engine="fast", served="Qwen/Qwen3.5-0.8B")], "Fast-r2"
            ),
            "another engine",
        ),
        (lambda f: with_release(f, [manifest("Main-r2", served="Other")], "Main-r2"), "serves"),
    ],
    ids=["unknown", "other-engine", "other-served-name"],
)
def test_an_engine_release_that_does_not_fit_refuses_the_feed(build, match):
    store = MemoryStore(clock=lambda: T0)
    apply_feed(store, feed(), now=T0)
    before = everything(store)

    with pytest.raises(ValueError, match=match):
        apply_feed(store, build(feed()), now=T0)

    assert everything(store) == before


def test_a_stored_release_of_another_engine_is_named_as_such():
    store = MemoryStore(clock=lambda: T0)
    apply_feed(store, with_release(feed(), [manifest("Main-r2")], "Main-r2"), now=T0)

    with pytest.raises(ValueError, match="another engine"):
        apply_feed(store, with_release(feed(), [], "Main-r2", engine="fast"), now=T0)


# ---- 7. a new release with the same served name is a new revision ------------------------


def test_an_engine_that_moves_to_another_release_gives_its_dependant_a_new_revision():
    slug = release_slug(SERVED)
    first = with_release(feed(), [manifest(slug)], slug)
    world = owned(first)
    number = newest(world)
    row = resolution(world, "llm-main")
    assert row.resolved_release == release_id("model", "main", slug)
    assert row.detail == {"alias": "main", "servedName": SERVED, "release": slug}

    second = with_release(feed(), [manifest(slug), manifest("Main-r2")], "Main-r2")
    assert apply_feed(world.store, second, now=T0)

    assert newest(world) == number + 1
    row = resolution(world, "llm-main")
    assert row.resolved_release == release_id("model", "main", "Main-r2")
    assert row.detail["release"] == "Main-r2" and row.detail["servedName"] == SERVED


def test_engine_release_is_the_named_release_else_the_one_adopted_for_the_served_name():
    assert derive.engine_release({"servedName": "A/b c", "release": "R2"}) == "R2"
    assert derive.engine_release({"servedName": "A/b c"}) == release_slug("A/b c")


# ---- 8. status ---------------------------------------------------------------------------


def test_status_shows_the_contract_of_each_tool_server_and_the_release_of_each_engine():
    f = with_release(
        with_contract(feed(), [tool("execute_sql"), tool("explain")]),
        [manifest("Main-r2")],
        "Main-r2",
    )
    world = owned(f)

    body = world.get("/v1/status", cluster=CLUSTER).json()

    servers = {s["name"]: s["contract"] for s in body["toolServers"]}
    recorded = world.store.deployment(f"tool-server:{CLUSTER}:-:{SERVER}").document["contract"]
    assert servers[SERVER] == {"state": "recorded", "digest": recorded["digest"], "tools": 2}
    assert servers["kubernetes-mcp"]["state"] == "none"
    releases = {m["name"]: m["release"] for m in body["models"]}
    assert releases == {"main": "Main-r2", "fast": None, "embeddings": None}

    item = world.get(f"/v1/items/tool-server/{SERVER}").json()
    assert item["contracts"][f"tool-server:{CLUSTER}:-:{SERVER}"]["tools"] == 2
    model = world.get("/v1/items/model/main").json()
    assert model["servedReleases"] == {f"model:{CLUSTER}:-:main": "Main-r2"}
