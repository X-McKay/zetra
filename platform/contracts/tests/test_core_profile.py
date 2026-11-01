import json
from pathlib import Path

import pytest
import yaml

from agent_platform.bundle import digest_hex
from agent_platform.podtemplate import render_pod_template
from tests.test_bundle import build


def test_legacy_policy_digest_is_unchanged(agent, release, cfg):
    assert (
        digest_hex(build(agent, release, cfg))
        == "a810d7b022f0fed3bec76f41bd1c15f6504c364a3b3432e8385b36ef50987750"
    )


def test_core_profile_is_part_of_signed_policy_identity(agent, release, cfg):
    legacy = digest_hex(build(agent, release, cfg))
    release["spec"]["runtimeProfile"] = "zetra-core-v1"
    core = build(agent, release, cfg)
    assert core["runtime"]["execution_profile"] == "zetra-core-v1"
    assert digest_hex(core) != legacy


@pytest.mark.parametrize("profile", ["", "legacy", "arbitrary", None, True])
def test_unknown_or_null_profile_cannot_silently_select_legacy(agent, release, cfg, profile):
    release["spec"]["runtimeProfile"] = profile
    with pytest.raises(ValueError, match="runtimeProfile"):
        build(agent, release, cfg)


def test_core_pod_template_carries_immutable_release_identity(agent, release, cfg):
    release["spec"]["runtimeProfile"] = "zetra-core-v1"
    bundle = build(agent, release, cfg)
    digest = digest_hex(bundle)
    release["spec"]["policySha256"] = digest
    pod = render_pod_template(agent, release, bundle, "sha256:" + digest, cfg)
    env = {v["name"]: v["value"] for v in pod["spec"]["containers"][0]["env"]}
    assert json.loads(env["AGENT_RELEASE_BINDING"]) == {
        "profile": "zetra-core-v1",
        "tenant": agent["metadata"]["namespace"],
        "agent": agent["metadata"]["name"],
        "release": release["metadata"]["name"],
        "image": release["spec"]["image"],
        "manifest_sha256": release["spec"]["manifestSha256"],
        "policy_sha256": digest,
    }
    assert "AGENT_CORE_BINDING" not in env  # exact Workflow/run/task belongs to the runner


def test_crd_core_profile_is_optional_without_a_default_and_immutable():
    path = (
        Path(__file__).resolve().parents[3]
        / "deploy/platform/crds/agentreleases.agentplatform.io.yaml"
    )
    spec = yaml.safe_load(path.read_text())["spec"]["versions"][0]["schema"]["openAPIV3Schema"][
        "properties"
    ]["spec"]
    profile = spec["properties"]["runtimeProfile"]
    assert profile == {"type": "string", "enum": ["zetra-core-v1"]}
    assert "runtimeProfile" not in spec["required"]
    rule = next(
        r["rule"]
        for r in spec["x-kubernetes-validations"]
        if r["message"] == "runtimeProfile is immutable"
    )
    assert (
        rule
        == "has(self.runtimeProfile) == has(oldSelf.runtimeProfile) && (!has(self.runtimeProfile) || self.runtimeProfile == oldSelf.runtimeProfile)"
    )
