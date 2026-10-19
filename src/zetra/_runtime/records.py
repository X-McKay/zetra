"""Strict, lossless JSON boundary encoding for the admitted ADK profile."""

import base64
import hashlib
import json
import math

from pydantic import BaseModel

_TAG = "__bridge_bytes__"


def _json(value):
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [_json(v) for v in value]
    if type(value) is dict and all(type(k) is str for k in value):
        return {k: _json(v) for k, v in value.items()}
    raise ValueError(f"unsupported canonical value: {type(value).__name__}")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _json(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def encode_value(value):
    if isinstance(value, bytes):
        return {_TAG: base64.b64encode(value).decode("ascii")}
    if isinstance(value, str):  # SDK string enums
        return str(value.value) if hasattr(value, "value") else value
    if type(value) is dict:
        if _TAG in value:
            raise ValueError("reserved wire tag")
        if not all(type(k) is str for k in value):
            raise ValueError("non-string object key")
        return {k: encode_value(v) for k, v in value.items()}
    if type(value) is list:
        return [encode_value(v) for v in value]
    return _json(value)


def decode_value(value):
    if type(value) is dict:
        if _TAG in value:
            if set(value) != {_TAG} or type(value[_TAG]) is not str:
                raise ValueError("invalid byte envelope")
            return base64.b64decode(value[_TAG], validate=True)
        return {k: decode_value(v) for k, v in value.items()}
    if type(value) is list:
        return [decode_value(v) for v in value]
    return _json(value)


def _field_mask(value):
    if isinstance(value, BaseModel):
        return {
            "fields": sorted(value.model_fields_set),
            "children": {
                k: _field_mask(getattr(value, k))
                for k, f in type(value).model_fields.items()
                if not f.exclude
            },
        }
    if isinstance(value, list):
        return [_field_mask(v) for v in value]
    if isinstance(value, dict):
        return {k: _field_mask(v) for k, v in value.items()}
    return None


def _restore_mask(value, mask):
    if isinstance(value, BaseModel):
        fields = mask["fields"]
        if not isinstance(fields, list) or not all(k in type(value).model_fields for k in fields):
            raise ValueError("invalid model field mask")
        value.model_fields_set.clear()
        value.model_fields_set.update(fields)
        for k, child in mask["children"].items():
            if k not in type(value).model_fields:
                raise ValueError("unknown masked field")
            _restore_mask(getattr(value, k), child)
    elif isinstance(value, list):
        if len(value) != len(mask):
            raise ValueError("invalid list field mask")
        for v, m in zip(value, mask):
            _restore_mask(v, m)
    elif isinstance(value, dict):
        if value.keys() != mask.keys():
            raise ValueError("invalid mapping field mask")
        for k, v in value.items():
            _restore_mask(v, mask[k])
    elif mask is not None:
        raise ValueError("invalid scalar field mask")


def encode_model(value: BaseModel) -> dict:
    # Full values retain in-place mutations of default lists; the mask retains
    # absent-vs-explicit semantics without dropping those mutated defaults.
    return {"data": encode_value(value.model_dump(mode="python")), "set_fields": _field_mask(value)}


def decode_model[M: BaseModel](cls: type[M], value: dict) -> M:
    try:
        if set(value) != {"data", "set_fields"}:
            raise ValueError("invalid model envelope")
        result = cls.model_validate(decode_value(value["data"]))
        _restore_mask(result, value["set_fields"])
        return result
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid model envelope") from exc


def describe(kind, executor, request, *, index, invocation="probe-invocation", manifest="probe-v1"):
    if type(index) is not int or index < 0:
        raise ValueError("invalid operation index")
    identity = {
        "kind": kind,
        "executor": executor,
        "executor_version": "1",
        "request": json.loads(canonical_bytes(request)),
        "manifest_digest": manifest,
    }
    return dict(
        identity,
        index=index,
        operation_id=f"{invocation}:{index:08d}",
        request_digest=digest(identity),
    )


def verify_operation(operation, *, manifest, index=None):
    """Recompute identity from canonical semantic input before authorizing execution."""
    try:
        invocation, _suffix = operation["operation_id"].rsplit(":", 1)
        expected = describe(
            operation["kind"],
            operation["executor"],
            operation["request"],
            index=operation["index"],
            invocation=invocation,
            manifest=manifest,
        )
        if operation != expected or (index is not None and operation["index"] != index):
            raise ValueError("operation identity mismatch")
        if operation["kind"] not in ("model", "tool") or operation["index"] >= 64:
            raise ValueError("operation outside admitted profile")
        return json.loads(canonical_bytes(operation))
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid operation descriptor") from exc


def thaw(value):
    from collections.abc import Mapping

    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value
