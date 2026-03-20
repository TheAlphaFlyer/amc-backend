"""Tests for the SSE client module."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from amc.sse_client import parse_sse_event, _debounced_flush


class TestParseSSEEvent:
    def test_simple_event(self):
        lines = ["id: 42", "data: {\"hook\":\"ServerCargoArrived\",\"timestamp\":1234}"]
        event_id, data = parse_sse_event(lines)
        assert event_id == "42"
        assert json.loads(data) == {"hook": "ServerCargoArrived", "timestamp": 1234}

    def test_data_only_no_id(self):
        lines = ["data: {\"hook\":\"ServerPassengerArrived\"}"]
        event_id, data = parse_sse_event(lines)
        assert event_id is None
        assert json.loads(data) == {"hook": "ServerPassengerArrived"}

    def test_multiline_data(self):
        lines = [
            "id: 5",
            "data: first line",
            "data: second line",
        ]
        event_id, data = parse_sse_event(lines)
        assert event_id == "5"
        assert data == "first line\nsecond line"

    def test_comment_lines_ignored(self):
        lines = [
            ": this is a comment",
            "id: 10",
            "data: {\"test\":true}",
        ]
        event_id, data = parse_sse_event(lines)
        assert event_id == "10"
        assert json.loads(data) == {"test": True}

    def test_empty_lines(self):
        event_id, data = parse_sse_event([])
        assert event_id is None
        assert data is None

    def test_no_data_field(self):
        lines = ["id: 99"]
        event_id, data = parse_sse_event(lines)
        assert event_id is None
        assert data is None

    def test_missed_events_comment(self):
        """The server sends ': missed_events' when events were dropped."""
        lines = [": missed_events"]
        event_id, data = parse_sse_event(lines)
        assert event_id is None
        assert data is None

    def test_whitespace_handling(self):
        lines = ["id:  7 ", "data:  {\"key\": \"value\"} "]
        event_id, data = parse_sse_event(lines)
        assert event_id == "7"
        assert json.loads(data) == {"key": "value"}


@pytest.mark.asyncio
async def test_debounced_flush_calls_process_events():
    buffer = [
        {"hook": "ServerCargoArrived", "timestamp": 1, "data": {}},
        {"hook": "ServerPassengerArrived", "timestamp": 2, "data": {}},
    ]
    with patch("amc.webhook.process_events", new_callable=AsyncMock) as mock_process:
        await _debounced_flush(buffer, None, None, None)

    mock_process.assert_awaited_once()
    args = mock_process.call_args
    assert len(args[0][0]) == 2
    assert buffer == []  # Buffer should be cleared


@pytest.mark.asyncio
async def test_debounced_flush_empty_buffer():
    buffer = []
    with patch("amc.webhook.process_events", new_callable=AsyncMock) as mock_process:
        await _debounced_flush(buffer, None, None, None)
    mock_process.assert_not_awaited()


@pytest.mark.asyncio
async def test_debounced_flush_handles_exception(caplog):
    buffer = [{"hook": "test", "timestamp": 1, "data": {}}]
    with patch(
        "amc.webhook.process_events",
        new_callable=AsyncMock,
        side_effect=ValueError("test error"),
    ):
        # Should not raise — errors are caught and logged
        await _debounced_flush(buffer, None, None, None)
    assert buffer == []  # Buffer still cleared even on error
