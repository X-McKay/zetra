import base64

import pytest

from zetra._runtime.artifacts import FileStore
from zetra._runtime.chunks import CHUNK_BYTES, accept_chunk


def test_large_transfer_and_duplicate_chunks_are_not_progress(tmp_path):
    source = FileStore(tmp_path / "source")
    target = FileStore(tmp_path / "target")
    data = b"x" * (1048576 + 100)
    ref = source.put(data)
    for offset in range(0, len(data), CHUNK_BYTES):
        envelope = {
            "ref": ref.model_dump(),
            "offset": offset,
            "data": base64.b64encode(data[offset : offset + CHUNK_BYTES]).decode(),
        }
        result = accept_chunk(target, envelope)
        assert accept_chunk(target, envelope) == result
    assert result["complete"] and target.get(ref) == data


def test_missing_or_changed_transfer_chunk_rejected(tmp_path):
    store = FileStore(tmp_path)
    data = b"x" * (CHUNK_BYTES * 2)
    import hashlib

    ref = {"sha256": hashlib.sha256(data).hexdigest(), "byte_length": len(data)}
    envelope = {
        "ref": ref,
        "offset": CHUNK_BYTES,
        "data": base64.b64encode(data[:CHUNK_BYTES]).decode(),
    }
    with pytest.raises(ValueError):
        accept_chunk(store, envelope)
    envelope["offset"] = 0
    accept_chunk(store, envelope)
    envelope["data"] = base64.b64encode(b"y" * CHUNK_BYTES).decode()
    with pytest.raises(ValueError):
        accept_chunk(store, envelope)
