import base64

import pytest

from zetra._runtime.artifacts import FileStore


def test_inline_batch_includes_small_objects_and_leaves_large_objects_for_transfer(tmp_path):
    from zetra._runtime.inline import inline_objects, verify_inline

    store = FileStore(tmp_path)
    small = store.put(b"candidate")
    large = store.put(b"x" * 131073)
    objects = [small.model_dump(), large.model_dump()]
    bundled = inline_objects(store, objects)
    assert verify_inline(objects, bundled) == {small.sha256: b"candidate"}
    assert store.get(large) == b"x" * 131073


@pytest.mark.parametrize("damage", ["hash", "unknown", "duplicate", "encoding", "budget"])
def test_inline_batch_rejects_corrupt_or_unbound_artifacts(tmp_path, damage):
    from zetra._runtime.inline import verify_inline

    store = FileStore(tmp_path)
    ref = store.put(b"ok")
    item = {"ref": ref.model_dump(), "data": base64.b64encode(b"ok").decode()}
    objects, inline = [ref.model_dump()], [item]
    if damage == "hash":
        item["data"] = base64.b64encode(b"no").decode()
    elif damage == "unknown":
        objects = []
    elif damage == "duplicate":
        inline.append(item)
    elif damage == "encoding":
        item["data"] = "%%!"
    else:
        ref = store.put(b"x" * 131073)
        objects = [ref.model_dump()]
        inline = [{"ref": ref.model_dump(), "data": base64.b64encode(b"x" * 131073).decode()}]
    with pytest.raises(ValueError):
        verify_inline(objects, inline)
