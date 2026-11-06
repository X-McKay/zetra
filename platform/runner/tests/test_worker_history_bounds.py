"""Streaming history caps stop before retaining or fetching the remainder."""

import hashlib
from types import SimpleNamespace as N
from unittest.mock import Mock

import pytest
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import WorkflowHistory

from runner.worker_operations import bounded_history
from runner.worker_versioning import GuardError


class Pages:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.fetched = 0
        self.current_page = None
        self.next_page_token = b"next"

    async def fetch_next_page(self):
        self.fetched += 1
        self.current_page = next(self.pages)
        self.next_page_token = b"next" if self.current_page else None


def handle(pages):
    stream = Pages(pages)
    return N(fetch_history_events=Mock(return_value=stream)), stream


@pytest.mark.asyncio
async def test_event_limit_stops_oversized_stream_early():
    h, stream = handle(
        [
            [HistoryEvent(event_id=1)],
            [HistoryEvent(event_id=2)],
            [HistoryEvent(event_id=3)],
            [HistoryEvent(event_id=4)],
        ]
    )
    with pytest.raises(GuardError, match="bound"):
        await bounded_history(h, "workflow", max_events=1)
    assert stream.fetched == 2


@pytest.mark.asyncio
async def test_serialized_byte_limit_stops_before_fetching_remaining_pages():
    big = HistoryEvent(
        event_id=2,
        workflow_execution_started_event_attributes={
            "input": {"payloads": [{"data": b"x" * 1000}]}
        },
    )
    h, stream = handle([[HistoryEvent(event_id=1)], [big], [HistoryEvent(event_id=3)]])
    with pytest.raises(GuardError, match="bound"):
        await bounded_history(h, "workflow", max_bytes=200)
    assert stream.fetched == 2


@pytest.mark.asyncio
async def test_exact_sdk_json_digest_preserves_opaque_payload_bytes():
    events = [
        HistoryEvent(event_id=1),
        HistoryEvent(
            event_id=2,
            workflow_execution_started_event_attributes={
                "input": {
                    "payloads": [
                        {"metadata": {"encoding": b"binary/encrypted"}, "data": b"\x00\xffopaque"}
                    ]
                }
            },
        ),
    ]
    h, stream = handle([[events[0]], [events[1]], []])
    history, digest = await bounded_history(h, "workflow")
    assert history.events == events
    assert (
        digest
        == hashlib.sha256(
            WorkflowHistory(workflow_id="workflow", events=events).to_json().encode()
        ).hexdigest()
    )
    assert (
        history.events[1].workflow_execution_started_event_attributes.input.payloads[0].data
        == b"\x00\xffopaque"
    )
    h.fetch_history_events.assert_called_once()


@pytest.mark.asyncio
async def test_empty_history_digest_is_exact_sdk_serialization():
    h, _ = handle([[]])
    history, digest = await bounded_history(h, "workflow")
    assert digest == hashlib.sha256(history.to_json().encode()).hexdigest()


@pytest.mark.asyncio
async def test_page_limit_stops_without_consuming_tail():
    h, stream = handle([[HistoryEvent(event_id=n)] for n in range(1, 10)])
    with pytest.raises(GuardError, match="bound"):
        await bounded_history(h, "workflow", max_pages=2)
    assert stream.fetched == 2


@pytest.mark.asyncio
async def test_json_expansion_is_capped_before_event_retention():
    event = HistoryEvent(
        event_id=1,
        workflow_execution_started_event_attributes={"workflow_type": {"name": "\u2603" * 50}},
    )
    exact_size = len(WorkflowHistory(workflow_id="workflow", events=[event]).to_json().encode())
    limit = (event.ByteSize() + exact_size) // 2
    assert event.ByteSize() + 32 < limit < exact_size
    h, stream = handle([[event], [HistoryEvent(event_id=2)]])
    with pytest.raises(GuardError, match="serialized-byte"):
        await bounded_history(h, "workflow", max_bytes=limit)
    assert stream.fetched == 1


@pytest.mark.asyncio
async def test_server_page_larger_than_requested_is_refused_before_retention():
    h, stream = handle([[HistoryEvent(event_id=n) for n in range(101)], []])
    with pytest.raises(GuardError, match="page"):
        await bounded_history(h, "workflow")
    assert stream.fetched == 1
