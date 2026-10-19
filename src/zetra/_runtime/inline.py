"""Bounded candidate artifact batches; data never selects accepted progress."""

import base64
import hashlib

from .artifacts import Ref

INLINE_BYTES = 131072


def inline_objects(store, objects):
    remaining = INLINE_BYTES
    result = []
    for raw in objects:
        ref = Ref.model_validate(raw)
        if ref.byte_length <= remaining:
            data = store.get(ref)
            result.append({"ref": raw, "data": base64.b64encode(data).decode("ascii")})
            remaining -= len(data)
    return result


def verify_inline(objects, bundled):
    if not isinstance(objects, list) or len(objects) > 16:
        raise ValueError("invalid candidate object list")
    expected = {}
    for raw in objects:
        ref = Ref.model_validate(raw)
        if ref.sha256 in expected:
            raise ValueError("duplicate candidate object")
        expected[ref.sha256] = ref
    if not isinstance(bundled, list) or len(bundled) > len(expected):
        raise ValueError("invalid inline batch")
    result = {}
    remaining = INLINE_BYTES
    for item in bundled:
        if not isinstance(item, dict) or set(item) != {"ref", "data"}:
            raise ValueError("invalid inline object")
        if not isinstance(item["data"], str):
            raise ValueError("invalid inline encoding")  # noqa: TRY004 -- protocol validation
        ref = Ref.model_validate(item["ref"])
        if expected.get(ref.sha256) != ref or ref.sha256 in result:
            raise ValueError("unbound or duplicate inline object")
        if ref.byte_length > remaining or len(item["data"]) > 4 * ((ref.byte_length + 2) // 3):
            raise ValueError("inline budget exceeded")
        data = base64.b64decode(item["data"], validate=True)
        if len(data) != ref.byte_length or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ValueError("inline hash or size mismatch")
        result[ref.sha256] = data
        remaining -= len(data)
    return result
