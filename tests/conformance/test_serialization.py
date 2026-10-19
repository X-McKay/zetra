import math

import pytest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from zetra._runtime import records


def test_json_is_stable_and_rejects_non_json():
    assert records.canonical_bytes({"b": [None, 1], "a": "é"}) == b'{"a":"\xc3\xa9","b":[null,1]}'
    for value in [math.nan, math.inf, {1: "invalid"}, object(), {1, 2}]:
        with pytest.raises(ValueError):
            records.canonical_bytes(value)


def test_response_roundtrip_preserves_full_provider_content():
    response = LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        name="inspect_repository", args={"path": "."}, id="call-1"
                    ),
                    thought_signature=b"\xff\x00sig",
                ),
            ],
        ),
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=11, candidates_token_count=4
        ),
        model_version="fixture-v1",
        custom_metadata={"explicit_null": None},
    )
    wire = records.encode_model(response)
    restored = records.decode_model(LlmResponse, wire)
    assert restored == response
    assert restored.content.parts[0].thought_signature == b"\xff\x00sig"
    restored.content.parts[0].function_call.args["path"] = "changed"
    assert records.decode_model(LlmResponse, wire).content.parts[0].function_call.args == {
        "path": "."
    }


def test_missing_and_null_remain_distinct():
    a = records.encode_model(LlmResponse())
    b = records.encode_model(LlmResponse(error_code=None))
    assert a != b
    assert records.decode_model(LlmResponse, a).model_fields_set == set()
    assert records.decode_model(LlmResponse, b).model_fields_set == {"error_code"}


def test_tool_business_error_roundtrip():
    data = {"error": {"code": "MISSING_FILE", "retryable": False}}
    assert records.decode_value(records.encode_value(data)) == data


def test_unknown_fields_and_reserved_tags_rejected():
    with pytest.raises(ValueError):
        records.decode_model(LlmResponse, {"unknown_field": True})
    with pytest.raises(ValueError):
        records.encode_value({"__bridge_bytes__": "collision"})


def test_mutated_default_lists_survive_encoding():
    from google.adk.events import Event
    from google.adk.sessions.session import Session

    session = Session(id="s", app_name="app", user_id="u")
    session.events.append(Event(author="user"))
    restored = records.decode_model(Session, records.encode_model(session))
    assert len(restored.events) == 1
    assert restored.events[0].author == "user"


def test_logical_identity_is_position_based_and_manifest_is_bound():
    from zetra._runtime.records import describe

    a = describe("tool", "inspect", {"path": "."}, index=0, invocation="inv", manifest="image-a")
    b = describe("tool", "inspect", {"path": "."}, index=1, invocation="inv", manifest="image-a")
    c = describe("tool", "inspect", {"path": "."}, index=0, invocation="inv", manifest="image-b")
    assert a["operation_id"] == "inv:00000000"
    assert b["operation_id"] == "inv:00000001"
    assert a["request_digest"] != c["request_digest"]
