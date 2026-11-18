"""What holds for every route whatever its handler does: who may call it, and the shape of
a refusal. These tests do not depend on any handler being built."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_registry import modes
from agent_registry.api import errors
from agent_registry.api.app import create_app
from agent_registry.api.auth import StaticVerifier
from agent_registry.config import Settings
from agent_registry.rules import Refusal
from agent_registry.store import Conflict, Missing, Unavailable
from agent_registry.store.memory import MemoryStore
from tests.world import DEP, TOKENS, World

READS = ["/v1/meta", "/v1/items", "/v1/deployments", "/v1/status", "/v1/events", "/v1/recovery"]
DESIRED = "/v1/clusters/kubani/desired"


def error(answer, status: int) -> dict:
    assert answer.status_code == status, answer.text
    body = answer.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "retryable"}
    return body["error"]


def test_health_needs_no_token_and_says_the_mode():
    world = World()

    assert world.get("/healthz", token=None).json() == {"status": "ok", "mode": "normal"}
    modes.enter_recovery(world.store, "a test", actor="test")
    assert world.get("/healthz", token=None).json() == {"status": "ok", "mode": "recovery"}


def test_health_says_when_the_database_cannot_be_reached():
    class Down(MemoryStore):
        def ping(self) -> None:
            raise Unavailable("connection refused to host db-1 as user registry")

    client = TestClient(create_app(Down(), Settings(), StaticVerifier(TOKENS)))

    found = error(client.get("/healthz"), 503)

    assert (found["code"], found["retryable"]) == ("unavailable", True)
    assert "db-1" not in found["message"]  # where the database is stays out of the answer


@pytest.mark.parametrize("path", [*READS, DESIRED, f"/v1/deployments/{DEP}"])
def test_no_route_answers_without_a_token(path):
    world = World()

    assert error(world.get(path, token=None), 401)["code"] == "not-signed-in"
    assert error(world.get(path, token="not-a-token"), 401)["code"] == "not-signed-in"
    wrong_scheme = world.client.get(path, headers={"Authorization": "Basic operator"})
    assert error(wrong_scheme, 401)["code"] == "not-signed-in"


@pytest.mark.parametrize("path", READS)
def test_the_read_routes_are_for_readers_and_operators(path):
    world = World()

    assert error(world.get(path, token="nobody"), 403)["code"] == "refused"
    assert error(world.get(path, token="provisioner"), 403)["code"] == "refused"
    for token in ("reader", "operator"):
        assert world.get(path, token=token).status_code not in (401, 403)


def test_the_cluster_routes_are_for_that_clusters_provisioner():
    world = World()

    for token in ("reader", "operator", "robot", "nobody"):
        assert error(world.get(DESIRED, token=token), 403)["code"] == "refused"
    other = world.get("/v1/clusters/elsewhere/desired", token="provisioner")
    assert error(other, 403)["code"] == "refused"
    assert world.get(DESIRED, token="provisioner").status_code not in (401, 403)


def test_authority_and_recovery_are_for_a_signed_in_person():
    world = World()
    paths = [
        f"/v1/deployments/{DEP}/authority/take",
        f"/v1/deployments/{DEP}/authority/return",
        "/v1/recovery/leave",
    ]

    for path in paths:
        for token in ("reader", "robot", "provisioner"):
            assert error(world.post(path, {"digest": "x"}, token=token), 403)["code"] == "refused"
        assert error(world.post(path, {"digest": "x"}, token=None), 401)["code"] == "not-signed-in"


def test_in_recovery_only_leaving_is_not_refused():
    world = World()
    world.seed()
    modes.enter_recovery(world.store, "a test", actor="test")

    for action in ("take", "return"):
        found = error(world.post(f"/v1/deployments/{DEP}/authority/{action}"), 409)
        assert (found["code"], found["retryable"]) == ("in-recovery", True)
    leave = world.post("/v1/recovery/leave", {"digest": "x"})
    assert leave.status_code != 409 or leave.json()["error"]["code"] != "in-recovery"


def test_a_request_with_an_unknown_field_is_refused():
    world = World()
    world.seed()

    answer = world.post(f"/v1/deployments/{DEP}/enabled", {"enabled": True, "force": True})

    found = error(answer, 400)
    assert found["code"] == "invalid-arguments"
    assert "force" in found["message"]


def test_every_failure_leaves_in_the_same_shape():
    app = FastAPI()
    errors.install(app)

    @app.get("/{what}")
    def fail(what: str):
        raise {
            "conflict": Conflict("two writes disagree"),
            "missing": Missing("no such thing"),
            "down": Unavailable("secret detail"),
            "unbuilt": NotImplementedError(),
            "stale": errors.ApiError("stale-approval", "the policy changed"),
            "forbidden": errors.ApiError.forbidden("not for you"),
            "rule": Refusal("wrong-path", "owned by the pull request path"),
        }[what]

    client = TestClient(app)

    assert error(client.get("/conflict"), 409)["code"] == "conflict"
    assert error(client.get("/missing"), 404)["code"] == "not-found"
    assert error(client.get("/down"), 503)["message"] == "the registry database cannot be reached"
    assert error(client.get("/unbuilt"), 501)["code"] == "unavailable"
    assert error(client.get("/stale"), 409) == {
        "code": "stale-approval",
        "message": "the policy changed",
        "retryable": False,
    }
    assert error(client.get("/forbidden"), 403)["code"] == "refused"
    assert error(client.get("/rule"), 409) == {
        "code": "wrong-path",
        "message": "owned by the pull request path",
        "retryable": False,
    }
    with pytest.raises(ValueError):
        errors.ApiError("made-up", "x")
