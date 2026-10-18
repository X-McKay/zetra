from zetra._runtime.openshell import OpenShellSession


def test_sandbox_labels_pin_release_image_and_policy():
    remote = OpenShellSession(
        None, None, session="one", manifest="release", key=b"x" * 32, agent_spec={"name": "agent"}
    )
    assert hasattr(remote, "release_labels")
    assert set(remote.release_labels) == {"zetra-release", "zetra-image", "zetra-policy"}


from types import SimpleNamespace

import pytest


def test_wrong_release_cannot_be_adopted():
    remote = OpenShellSession(
        None, None, session="one", manifest="release", key=b"x" * 32, agent_spec={"name": "agent"}
    )
    with pytest.raises(ValueError, match="release"):
        remote.verify_release(SimpleNamespace(labels={}))
    remote.verify_release(SimpleNamespace(labels=remote.release_labels))
