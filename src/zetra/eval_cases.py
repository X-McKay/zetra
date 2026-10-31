"""Bounded, data-only evaluation cases and deterministic evidence assertions."""

import json
import math

from .manifest import project_file, relative_path

MAX_SUITE_BYTES = 4_000_000
MAX_CASES = 1_000
MAX_ASSERTIONS = 100
MAX_TEXT = 65_536


def _text(value, *, maximum=MAX_TEXT):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("expected nonempty bounded text")
    return value


def _number(value, *, integer=False):
    return (
        type(value) in ((int,) if integer else (int, float))
        and value >= 0
        and (type(value) is int or math.isfinite(value))
    )


def _json_value(value, depth=0):
    if depth > 20:
        raise ValueError("arguments nested too deeply")
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, str) and len(value) <= MAX_TEXT:
        return
    if type(value) is list and len(value) <= 1_000:
        for child in value:
            _json_value(child, depth + 1)
        return
    if type(value) is dict and len(value) <= 1_000:
        for key, child in value.items():
            _text(key)
            _json_value(child, depth + 1)
        return
    raise ValueError("invalid JSON argument value")


def _assertion(value):
    if type(value) is not dict:
        raise ValueError("expected assertion object")
    kind = value.get("type")
    fields = {
        "text_contains": {"type", "value"},
        "text_not_contains": {"type", "value"},
        "file_contains": {"type", "path", "value"},
        "file_exists": {"type", "path"},
        "tool_called": {"type", "tool"},
        "tool_forbidden": {"type", "tool"},
        "tool_before": {"type", "tool", "before"},
        "max_steps": {"type", "maximum"},
        "max_duration_ms": {"type", "maximum"},
    }
    if not isinstance(kind, str) or kind not in fields:
        raise ValueError("unknown assertion type")
    expected = fields[kind]
    if kind == "tool_called" and "arguments" in value:
        expected = expected | {"arguments"}
    if set(value) != expected:
        raise ValueError("invalid assertion fields")
    for key in ("value", "tool", "before"):
        if key in value:
            _text(value[key])
    if "path" in value:
        relative_path(_text(value["path"]))
    if "arguments" in value:
        if type(value["arguments"]) is not dict:
            raise ValueError("tool arguments must be an object")
        _json_value(value["arguments"])
    if "maximum" in value and not _number(value["maximum"], integer=kind == "max_steps"):
        raise ValueError("maximum must be finite and nonnegative")
    if kind == "tool_before" and value["tool"] == value["before"]:
        raise ValueError("tool_before requires distinct tools")
    return dict(value)


def _case(value):
    if type(value) is not dict:
        raise ValueError("expected evaluation case object")
    if set(value) == {"id", "input", "contains", "files"}:
        if type(value["files"]) is not list or len(value["files"]) > MAX_ASSERTIONS - 1:
            raise ValueError("invalid legacy files")
        assertions = [{"type": "text_contains", "value": value["contains"]}]
        assertions += [{"type": "file_exists", "path": path} for path in value["files"]]
    elif (
        set(value) == {"schema_version", "id", "input", "assertions"}
        and type(value["schema_version"]) is int
        and value["schema_version"] == 2
    ):
        assertions = value["assertions"]
    else:
        raise ValueError("invalid evaluation case fields or version")
    _text(value["id"], maximum=128)
    _text(value["input"])
    if type(assertions) is not list or not 1 <= len(assertions) <= MAX_ASSERTIONS:
        raise ValueError("expected nonempty bounded assertions")
    return {
        "schema_version": 2,
        "id": value["id"],
        "input": value["input"],
        "assertions": [_assertion(assertion) for assertion in assertions],
    }


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def load_cases(root, agent):
    """Read and validate the entire suite before callers execute any case."""
    if len(relative_path(agent).parts) != 1:
        raise ValueError("invalid agent name")
    path = project_file(root, f"evals/{agent}/cases.jsonl")
    if path.stat().st_size > MAX_SUITE_BYTES:
        raise ValueError("evaluation suite too large")
    with path.open("rb") as stream:
        raw = stream.read(MAX_SUITE_BYTES + 1)
    if len(raw) > MAX_SUITE_BYTES:
        raise ValueError("evaluation suite too large")
    try:
        lines = raw.decode("utf-8").splitlines()
        if not 1 <= len(lines) <= MAX_CASES:
            raise ValueError("expected nonempty bounded evaluation suite")
        rows = [_case(json.loads(line, object_pairs_hook=_unique_object)) for line in lines]
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid evaluation suite encoding or nesting") from exc
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("duplicate evaluation case ID")
    return rows, raw


def _subset(expected, actual):
    if type(expected) is dict:
        return type(actual) is dict and all(
            key in actual and _subset(value, actual[key]) for key, value in expected.items()
        )
    if type(expected) is list:
        return (
            type(actual) is list
            and len(expected) == len(actual)
            and all(_subset(left, right) for left, right in zip(expected, actual, strict=True))
        )
    return type(expected) is type(actual) and expected == actual


def _tools(result):
    events = result.get("trajectory")
    if type(events) is not list:
        return None
    indices = set()
    for event in events:
        if (
            type(event) is not dict
            or not _number(event.get("index"), integer=True)
            or event["index"] in indices
            or event.get("kind") not in ("tool", "model")
            or not isinstance(event.get("executor"), str)
            or not event["executor"]
            or event.get("status") not in ("succeeded", "known_error", "outcome_unknown")
            or (event["kind"] == "tool" and type(event.get("arguments")) is not dict)
        ):
            return None
        indices.add(event["index"])
    return [event for event in events if event["kind"] == "tool"]


def _passes(assertion, result):
    kind = assertion["type"]
    if kind.startswith("text_"):
        output = result.get("result")
        text = output.get("final_text") if type(output) is dict else None
        if not isinstance(text, str):
            return False
        found = assertion["value"] in text
        return found if kind == "text_contains" else not found
    if kind == "file_exists":
        names = result.get("files")
        return type(names) is list and assertion["path"] in names
    if kind == "file_contains":
        contents = result.get("file_contents")
        text = contents.get(assertion["path"]) if type(contents) is dict else None
        return isinstance(text, str) and assertion["value"] in text
    if kind.startswith("max_"):
        metrics = result.get("metrics")
        field = "steps" if kind == "max_steps" else "duration_ms"
        value = metrics.get(field) if type(metrics) is dict else None
        return _number(value, integer=kind == "max_steps") and value <= assertion["maximum"]
    tools = _tools(result)
    if tools is None:
        return False
    matching = [event for event in tools if event["executor"] == assertion["tool"]]
    if kind == "tool_forbidden":
        return not matching
    matching = [event for event in matching if event["status"] == "succeeded"]
    if kind == "tool_called":
        return any(
            _subset(assertion.get("arguments", {}), event["arguments"]) for event in matching
        )
    other = [
        event["index"]
        for event in tools
        if event["executor"] == assertion["before"] and event["status"] == "succeeded"
    ]
    return bool(matching and other and min(event["index"] for event in matching) < min(other))


def grade(case, result):
    """Grade declared checks; missing or malformed observed evidence fails closed."""
    case = _case(case)
    if type(result) is not dict:
        result = {}
    rows = []
    for assertion in case["assertions"]:
        passed = bool(_passes(assertion, result))
        rows.append(
            {
                "type": assertion["type"],
                "passed": passed,
                "reason": "assertion satisfied"
                if passed
                else "assertion failed or evidence unavailable",
            }
        )
    return rows
