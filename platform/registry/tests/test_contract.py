"""The contract files say what the code says."""

from __future__ import annotations

import json
from pathlib import Path

from agent_registry import cli
from agent_registry.api import schemas as s
from agent_registry.config import ANNOTATION_PREFIX, FIELD_MANAGER
from agent_registry.derive import live_generation
from tests.world import DEP, fixture_documents

CONTRACT = Path(__file__).parents[1] / "contract"


def example(name: str) -> dict:
    return json.loads((CONTRACT / "examples" / name).read_text())


def test_the_committed_interface_is_the_one_the_code_serves():
    # Regenerate with: uv run agent-registry openapi --write contract/openapi.json
    assert json.loads((CONTRACT / "openapi.json").read_text()) == cli.openapi_document()


def test_the_desired_example_is_a_valid_answer():
    desired = s.DesiredOut.model_validate(example("desired.json"))

    assert desired.field_manager == FIELD_MANAGER
    (deployment,) = desired.deployments
    assert deployment.deployment == DEP
    assert [o.kind for o in deployment.objects] == ["Agent"] + ["AgentRelease"] * 3
    for projected in deployment.objects:
        document = projected.document
        assert set(document) == {"apiVersion", "kind", "metadata", "spec"}
        assert set(document["metadata"]) == {"name", "namespace", "annotations"}
        assert (document["kind"], document["metadata"]["name"]) == (projected.kind, projected.name)
        assert document["metadata"]["namespace"] == projected.namespace in desired.namespaces
        assert all(key.startswith(ANNOTATION_PREFIX) for key in document["metadata"]["annotations"])
        assert live_generation(document) == projected.generation <= desired.generation


def test_the_desired_example_projects_imported_releases_as_they_were():
    """Taking authority changes nothing on the cluster but annotations."""
    desired = example("desired.json")
    projected = {o["name"]: o["document"] for o in desired["deployments"][0]["objects"]}
    _, releases = fixture_documents()

    for manifest in releases:
        assert projected[manifest["metadata"]["name"]]["spec"] == manifest["spec"]
    newest = desired["deployments"][0]["objects"][-1]["document"]
    assert newest["spec"]["approval"] == {"approved": False}


def test_the_observations_example_is_a_valid_report_of_the_desired_example():
    report = s.ObservationsIn.model_validate(example("observations.json"))
    desired = s.DesiredOut.model_validate(example("desired.json"))
    wanted = {(o.kind.lower(), o.namespace, o.name) for o in desired.deployments[0].objects}

    assert {(o.subject_kind, o.namespace, o.name) for o in report.objects} == wanted
    assert {(p.kind.lower(), p.namespace, p.name) for p in report.projections} == wanted
    assert {(x.subject_kind, x.namespace, x.name) for x in report.subjects} == {
        (x.subject_kind, x.namespace, x.name) for x in desired.observe
    }
    assert report.last_generation == desired.generation
    for seen in report.objects:
        assert set(seen.document) == {"metadata", "spec", "status"}
        assert set(seen.document["metadata"]) == {
            "name",
            "namespace",
            "uid",
            "resourceVersion",
            "annotations",
        }
