"""SSE client for real-time event streaming from the C++ mod webserver.

Replaces the polling-based monitor_webhook cron when WEBHOOK_SSE_ENABLED=1.
Connects to /events/stream on the management port, parses SSE frames,
buffers events, and feeds them to process_events() in batches.
"""

import asyncio
import json
import logging

import aiohttp
from django.conf import settings

logger = logging.getLogger("amc.sse")

# Debounce: flush buffered events after this much silence
FLUSH_DEBOUNCE_SECONDS = 0.5

# Reconnect backoff bounds
INITIAL_BACKOFF = 1
MAX_BACKOFF = 30


def parse_sse_event(raw_lines: list[str]) -> tuple[str | None, str | None]:
    """Parse a single SSE event block (lines between blank lines).

    Returns (event_id, data_string) or (None, None) if no data.
    """
    event_id = None
    data_parts = []

    for line in raw_lines:
        if line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("data:"):
            data_parts.append(line[5:].strip())
        elif line.startswith(":"):
            # SSE comment — ignore (but could log for debugging)
            pass

    if data_parts:
        return event_id, "\n".join(data_parts)
    return None, None


async def run_sse_listener(ctx):
    """Long-running SSE listener task.

    Connects to the mod management server's /events/stream endpoint,
    receives events in real-time, buffers them, and flushes to
    process_events() after a short debounce.

    On disconnect, reconnects with exponential backoff and sends
    Last-Event-ID to replay missed events.
    """

    base_url = settings.WEBHOOK_SERVER_API_URL
    http_client = ctx.get("http_client")
    http_client_mod = ctx.get("http_client_mod")
    discord_client = ctx.get("discord_client")

    last_event_id = "0"
    backoff = INITIAL_BACKOFF

    timeout = aiohttp.ClientTimeout(
        total=None,  # No total timeout — SSE is long-lived
        sock_connect=10,
        sock_read=None,  # No read timeout — server may be quiet
    )

    while True:
        try:
            async with aiohttp.ClientSession(
                base_url=base_url, timeout=timeout
            ) as session:
                headers = {}
                if last_event_id != "0":
                    headers["Last-Event-ID"] = last_event_id

                logger.info(
                    "SSE connecting to %s/events/stream (Last-Event-ID: %s)",
                    base_url, last_event_id,
                )

                async with session.get(
                    "/events/stream", headers=headers
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "SSE endpoint returned %s, retrying in %ss",
                            resp.status, backoff,
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, MAX_BACKOFF)
                        continue

                    logger.info("SSE connected")
                    backoff = INITIAL_BACKOFF  # Reset on successful connect

                    event_buffer = []
                    current_lines: list[str] = []
                    flush_task: asyncio.Task | None = None

                    async for raw_line in resp.content:
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")

                        if line == "":
                            # Blank line = end of SSE event block
                            if current_lines:
                                event_id, data = parse_sse_event(current_lines)
                                current_lines = []
                                if data:
                                    if event_id:
                                        last_event_id = event_id
                                    try:
                                        event_obj = json.loads(data)
                                        event_buffer.append(event_obj)
                                    except json.JSONDecodeError:
                                        logger.warning(
                                            "SSE: invalid JSON data: %s", data[:200]
                                        )

                                    # Schedule a flush after debounce
                                    if flush_task and not flush_task.done():
                                        flush_task.cancel()
                                    flush_task = asyncio.create_task(
                                        _debounced_flush(
                                            event_buffer,
                                            http_client,
                                            http_client_mod,
                                            discord_client,
                                        )
                                    )
                        else:
                            current_lines.append(line)

            # If we get here, the response stream ended cleanly
            logger.info("SSE stream ended, reconnecting in %ss", backoff)

        except asyncio.CancelledError:
            logger.info("SSE listener shutting down")
            return

        except (aiohttp.ClientError, OSError) as e:
            logger.warning("SSE connection error: %s, retrying in %ss", e, backoff)

        except Exception:
            logger.exception("SSE unexpected error, retrying in %ss", backoff)

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


async def _debounced_flush(event_buffer, http_client, http_client_mod, discord_client):
    """Wait for the debounce period, then flush all buffered events."""
    from amc.webhook import process_events

    await asyncio.sleep(FLUSH_DEBOUNCE_SECONDS)

    if not event_buffer:
        return

    # Drain the buffer
    events = list(event_buffer)
    event_buffer.clear()

    logger.info("SSE flushing %d events", len(events))
    try:
        await process_events(events, http_client, http_client_mod, discord_client)
    except Exception:
        logger.exception("SSE: error processing %d events", len(events))
