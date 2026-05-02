"""Debug/error forwarding to Discord

This module forwards those records to a single Discord channel (`settings.DISCORD_LOGS_CHANNEL_ID`) so we can spot regressions

How it works
------------
Two entry points:

1. Explicit reports (preferred for known degraded paths):

       from amc import error_reporter
       error_reporter.report_warning(
           subject="wealth lookup failed",
           message=str(e),
           context={"character": character.name, "guid": str(character.guid)},
       )

   ...or for caught exceptions:

       try:
           ...
       except Exception as e:
           error_reporter.report_exception(
               e,
               subject="wealth lookup failed",
               context={"character": character.name},
           )

2. Implicit forwarding via the standard logging stack: any
   `logger.warning(...)` / `logger.error(...)` / `logger.exception(...)`
   on the `amc` or `amc_finance` loggers (and their children) is also
   forwarded if `DiscordLogHandler` is attached at worker startup.

   This means existing code does not need to change to gain visibility.

Cross-process boundary
----------------------
Discord runs on its own event loop inside the arq worker process. Any
forwarding is dispatched via `asyncio.run_coroutine_threadsafe(...)`
onto `discord_client.loop`, mirroring the pattern in
`amc.tasks._process_discord_queue` and `amc.events.send_event_embeds`.

The Django ASGI process does not have a Discord client and will fall
back to standard logging only — `_discord_client_ref` stays None there.

Safety
------
- All public entry points swallow internal failures (a broken reporter
  must NOT take down the caller — especially not when the caller is
  itself logging an error).
- Dedup window suppresses repeats of the same `subject` for
  `_DEDUP_WINDOW_SECS`, posting a single "(repeated N times)" follow-up
  when the window closes. This keeps Discord rate-limit happy when a
  bad config or downstream outage causes a thrashing failure.
- Embed bodies are truncated to Discord's 4096-char description limit.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
import traceback
from typing import TYPE_CHECKING, Any

from django.conf import settings

if TYPE_CHECKING:
    from amc.discord_client import AMCDiscordBot

logger = logging.getLogger(__name__)

_discord_client_ref: "AMCDiscordBot | None" = None

_dedup_lock = threading.Lock()
_dedup_state: dict[str, dict[str, Any]] = {}
_DEDUP_WINDOW_SECS = 60.0

_in_emit = threading.local()

# Hard upper bound on Discord embed description length.
_EMBED_DESC_MAX = 4000




def set_discord_client(client: "AMCDiscordBot | None") -> None:

    # Calling as a failsafe
    global _discord_client_ref
    _discord_client_ref = client


def _level_color(level: int) -> int:
    if level >= logging.CRITICAL:
        return 0x8B0000  # dark red
    if level >= logging.ERROR:
        return 0xE74C3C  # red
    if level >= logging.WARNING:
        return 0xF1C40F  # yellow
    if level >= logging.INFO:
        return 0x3498DB  # blue
    return 0x95A5A6  # grey (debug)


def _level_name(level: int) -> str:
    return logging.getLevelName(level) if level else "LOG"


def _truncate(text: str, limit: int = _EMBED_DESC_MAX) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…(truncated)"


def _format_context(context: dict[str, Any] | None) -> str:
    if not context:
        return ""
    lines = []
    for k, v in context.items():
        try:
            sv = str(v)
        except Exception:
            sv = "<unrepr>"
        if len(sv) > 200:
            sv = sv[:200] + "…"
        lines.append(f"**{k}**: `{sv}`")
    return "\n".join(lines)


def _dedup_key(subject: str, level: int) -> str:
    return hashlib.sha1(f"{level}:{subject}".encode("utf-8")).hexdigest()


def _should_send(key: str) -> tuple[bool, int]:
    """
    Decide whether to emit now.

    - send_now=True, suppressed=0   -> first occurrence in window, send fresh embed
    - send_now=False, suppressed=0  -> within window, increment counter, drop
    - send_now=True, suppressed=N   -> window expired with N drops; emit follow-up
    """
    now = time.monotonic()
    with _dedup_lock:
        entry = _dedup_state.get(key)
        if entry is None:
            _dedup_state[key] = {"window_start": now, "count": 0}
            return True, 0
        if now - entry["window_start"] < _DEDUP_WINDOW_SECS:
            entry["count"] += 1
            return False, 0
        # Window expired. Emit follow-up summary if anything was suppressed,
        # then start a fresh window.
        suppressed = entry["count"]
        _dedup_state[key] = {"window_start": now, "count": 0}
        return True, suppressed


def _build_payload(
    subject: str,
    message: str,
    level: int,
    context: dict[str, Any] | None,
    traceback_text: str | None,
    suppressed_count: int,
) -> dict[str, Any]:
    """Build a serializable embed payload (built later on the Discord loop)."""
    parts = []
    if message:
        parts.append(message)
    ctx_block = _format_context(context)
    if ctx_block:
        parts.append("\n" + ctx_block)
    if traceback_text:
        parts.append("\n```\n" + traceback_text.strip() + "\n```")
    if suppressed_count > 0:
        parts.append(
            f"\n_Plus {suppressed_count} suppressed repeat(s) in the last "
            f"{int(_DEDUP_WINDOW_SECS)}s._"
        )
    description = _truncate("\n".join(parts).strip())
    return {
        "title": f"[{_level_name(level)}] {subject}"[:256],
        "description": description,
        "color": _level_color(level),
    }


async def _post_embed(channel_id: int, payload: dict[str, Any]) -> None:
    """Runs on the Discord event loop. Posts the embed to the logs channel."""
    import discord

    client = _discord_client_ref
    if client is None or not channel_id:
        return
    try:
        if not client.is_ready():
            await client.wait_until_ready()
        channel = client.get_channel(channel_id)
        if channel is None:
            channel = await client.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        embed = discord.Embed(
            title=payload["title"],
            description=payload["description"],
            color=payload["color"],
        )
        await channel.send(embed=embed)
    except Exception:
        # Last-resort fallback: don't recurse through Discord forwarder.
        logger.exception("error_reporter: failed to post embed to Discord")


def _dispatch(payload: dict[str, Any]) -> None:
    """Schedule the embed post onto the Discord loop. Never raises."""
    client = _discord_client_ref
    channel_id = int(getattr(settings, "DISCORD_LOGS_CHANNEL_ID", 0) or 0)
    if client is None or not channel_id:
        return
    loop = getattr(client, "loop", None)
    if not isinstance(loop, asyncio.AbstractEventLoop) or loop.is_closed():
        return
    try:
        asyncio.run_coroutine_threadsafe(_post_embed(channel_id, payload), loop)
    except Exception:
        # Swallow — already logging via stdlib logging in the caller.
        pass


def _report(
    subject: str,
    message: str,
    level: int,
    context: dict[str, Any] | None = None,
    traceback_text: str | None = None,
) -> None:
    if getattr(_in_emit, "active", False):
        # Re-entrancy from inside the log handler. Drop to avoid loops.
        return
    _in_emit.active = True
    try:
        key = _dedup_key(subject, level)
        send_now, suppressed = _should_send(key)
        if not send_now:
            return
        payload = _build_payload(
            subject=subject,
            message=message,
            level=level,
            context=context,
            traceback_text=traceback_text,
            suppressed_count=suppressed,
        )
        _dispatch(payload)
    except Exception:
        # Reporter must never break callers.
        pass
    finally:
        _in_emit.active = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def report_warning(
    subject: str,
    message: str = "",
    *,
    context: dict[str, Any] | None = None,
) -> None:
    """Forward a non-exception warning to the Discord logs channel."""
    _report(subject, message, logging.WARNING, context=context)


def report_error(
    subject: str,
    message: str = "",
    *,
    context: dict[str, Any] | None = None,
) -> None:
    """Forward a non-exception error to the Discord logs channel."""
    _report(subject, message, logging.ERROR, context=context)


def report_exception(
    exc: BaseException,
    *,
    subject: str,
    context: dict[str, Any] | None = None,
    level: int = logging.ERROR,
) -> None:
    """Forward a caught exception (with traceback) to the Discord logs channel."""
    try:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        tb = repr(exc)
    message = f"{type(exc).__name__}: {exc}"
    _report(subject, message, level, context=context, traceback_text=tb)


# ---------------------------------------------------------------------------
# Logging integration
# ---------------------------------------------------------------------------


class DiscordLogHandler(logging.Handler):
    """A `logging.Handler` that forwards WARNING+ records to Discord.

    Attach via `amc_backend.worker.startup` after the bot is constructed.
    Threshold defaults to WARNING; pass `level=logging.ERROR` to be quieter.
    """

    def __init__(self, level: int = logging.WARNING) -> None:
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        if getattr(_in_emit, "active", False):
            return
        try:
            subject = f"{record.name}: {record.getMessage()[:80]}"
            tb_text = None
            if record.exc_info:
                tb_text = "".join(traceback.format_exception(*record.exc_info))
            _report(
                subject=subject,
                message=record.getMessage(),
                level=record.levelno,
                context={
                    "logger": record.name,
                    "module": f"{record.module}.{record.funcName}:{record.lineno}",
                },
                traceback_text=tb_text,
            )
        except Exception:
            # Logging handlers must never raise.
            self.handleError(record)
