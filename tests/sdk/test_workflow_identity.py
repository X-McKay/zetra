import pytest

from zetra.client import verify_start


def test_wrong_owner_cannot_attach_to_same_workflow():
    original = {"session": "one", "owner_key_id": "a", "release_id": "release"}
    with pytest.raises(ValueError, match="owner"):
        verify_start(original, {**original, "owner_key_id": "b"})
    verify_start(original, dict(original))
