"""Tests for the SSE client module."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from amc.sse_client import (
    parse_sse_event,
    _flush_loop,
    FLUSH_DEBOUNCE_SECONDS,
    FLUSH_MAX_BATCH_SIZE,
)


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


def _make_event(i=0):
    return {"hook": f"Event{i}", "data": {}, "timestamp": 1000 + i}


@pytest.mark.asyncio
async def test_debounce_flushes_after_silence():
    """Events arriving close together should be batched into a single flush."""
    mock_process = AsyncMock()
    buffer: list[dict] = []
    signal = asyncio.Event()

    with patch("amc.webhook.process_events", mock_process):
        task = asyncio.create_task(
            _flush_loop(buffer, signal, None, None, None)
        )

        # Push 3 events 50ms apart
        for i in range(3):
            buffer.append(_make_event(i))
            signal.set()
            await asyncio.sleep(0.05)

        # Wait for debounce to expire
        await asyncio.sleep(FLUSH_DEBOUNCE_SECONDS + 0.2)

        assert mock_process.call_count == 1
        assert len(mock_process.call_args[0][0]) == 3

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_ceiling_forces_flush_during_burst():
    """Events arriving continuously should still flush at the ceiling."""
    mock_process = AsyncMock()
    buffer: list[dict] = []
    signal = asyncio.Event()

    with patch("amc.webhook.process_events", mock_process):
        task = asyncio.create_task(
            _flush_loop(buffer, signal, None, None, None)
        )

        # Push events every 200ms for 3 seconds (past the 2s ceiling)
        for i in range(15):
            buffer.append(_make_event(i))
            signal.set()
            await asyncio.sleep(0.2)

        # Wait for any final debounce
        await asyncio.sleep(FLUSH_DEBOUNCE_SECONDS + 0.2)

        # Should have flushed more than once (ceiling forces flush mid-burst)
        assert mock_process.call_count >= 2

        # All events must have been processed
        total_events = sum(
            len(call_args[0][0]) for call_args in mock_process.call_args_list
        )
        assert total_events == 15

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_batch_cap_forces_immediate_flush():
    """Pushing >= FLUSH_MAX_BATCH_SIZE events at once should flush immediately."""
    mock_process = AsyncMock()
    buffer: list[dict] = []
    signal = asyncio.Event()

    with patch("amc.webhook.process_events", mock_process):
        task = asyncio.create_task(
            _flush_loop(buffer, signal, None, None, None)
        )

        # Push exactly FLUSH_MAX_BATCH_SIZE events at once
        for i in range(FLUSH_MAX_BATCH_SIZE):
            buffer.append(_make_event(i))
        signal.set()

        # Give just a tiny bit of time for the loop to process
        await asyncio.sleep(0.1)

        assert mock_process.call_count == 1
        assert len(mock_process.call_args[0][0]) == FLUSH_MAX_BATCH_SIZE

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_empty_buffer_no_flush():
    """Signal without events in the buffer should not call process_events."""
    mock_process = AsyncMock()
    buffer: list[dict] = []
    signal = asyncio.Event()

    with patch("amc.webhook.process_events", mock_process):
        task = asyncio.create_task(
            _flush_loop(buffer, signal, None, None, None)
        )

        signal.set()
        await asyncio.sleep(FLUSH_DEBOUNCE_SECONDS + 0.2)

        assert mock_process.call_count == 0

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_multiple_separate_batches():
    """Separate bursts with silence between them should result in separate flushes."""
    mock_process = AsyncMock()
    buffer: list[dict] = []
    signal = asyncio.Event()

    with patch("amc.webhook.process_events", mock_process):
        task = asyncio.create_task(
            _flush_loop(buffer, signal, None, None, None)
        )

        # First burst
        buffer.append(_make_event(0))
        signal.set()
        await asyncio.sleep(FLUSH_DEBOUNCE_SECONDS + 0.2)
        assert mock_process.call_count == 1
        assert len(mock_process.call_args[0][0]) == 1

        # Second burst
        buffer.append(_make_event(1))
        buffer.append(_make_event(2))
        signal.set()
        await asyncio.sleep(FLUSH_DEBOUNCE_SECONDS + 0.2)
        assert mock_process.call_count == 2
        assert len(mock_process.call_args[0][0]) == 2

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_flush_handles_process_exception(caplog):
    """Errors in process_events should be caught, buffer still cleared."""
    mock_process = AsyncMock(side_effect=ValueError("test error"))
    buffer: list[dict] = []
    signal = asyncio.Event()

    with patch("amc.webhook.process_events", mock_process):
        task = asyncio.create_task(
            _flush_loop(buffer, signal, None, None, None)
        )

        buffer.append(_make_event(0))
        signal.set()
        await asyncio.sleep(FLUSH_DEBOUNCE_SECONDS + 0.2)

        # process_events was called (and raised), but loop continues
        assert mock_process.call_count == 1
        assert buffer == []  # Buffer was drained before the call

        # Push another event — loop should still be alive
        buffer.append(_make_event(1))
        signal.set()
        await asyncio.sleep(FLUSH_DEBOUNCE_SECONDS + 0.2)
        assert mock_process.call_count == 2

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
