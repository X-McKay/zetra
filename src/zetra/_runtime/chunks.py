"""Resumable byte transfer only; staging files never select an accepted tape head."""

import base64
import os

from .artifacts import Ref

CHUNK_BYTES = 131072


def accept_chunk(store, envelope):
    ref = Ref.model_validate(envelope["ref"])
    offset = envelope["offset"]
    if type(offset) is not int or offset < 0 or offset % CHUNK_BYTES:
        raise ValueError("invalid chunk offset")
    chunk = base64.b64decode(envelope["data"], validate=True)
    if len(chunk) > CHUNK_BYTES or offset + len(chunk) > ref.byte_length:
        raise ValueError("invalid chunk size")
    if offset + len(chunk) != ref.byte_length and len(chunk) != CHUNK_BYTES:
        raise ValueError("incomplete intermediate chunk")
    try:
        store.get(ref)
        return {"complete": True, "ref": ref.model_dump()}
    except ValueError:
        pass
    path = store.root / (".partial-" + ref.sha256)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        size = os.fstat(stream.fileno()).st_size
        if size < offset:
            raise ValueError("missing prior chunk")
        if size > offset:
            stream.seek(offset)
            if stream.read(len(chunk)) != chunk:
                raise ValueError("conflicting duplicate chunk")
        else:
            stream.seek(offset)
            stream.write(chunk)
            stream.flush()
        if os.fstat(stream.fileno()).st_size == ref.byte_length:
            stream.seek(0)
            if store.put(stream.read(ref.byte_length + 1)) != ref:
                raise ValueError("artifact hash mismatch")
            path.unlink()
            return {"complete": True, "ref": ref.model_dump()}
    return {"complete": False}
