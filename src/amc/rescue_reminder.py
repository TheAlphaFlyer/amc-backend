"""Rescue reminder loop.

Periodically checks for unresponded rescue requests (no responders) and
re-announces them on Discord and in-game every minute until someone responds
or the request expires (after RESCUE_EXPIRY_MINUTES).

Runs as a long-lived asyncio task started from the Discord bot's setup_hook,
following the same pattern as auto_arrest.py.
"""

import asyncio
import logging

from datetime import timedelta

from django.utils import timezone
from django.conf import settings
from django.utils.translation import gettext as _

from amc.models import RescueRequest
from amc.game_server import announce

logger = logging.getLogger("amc.rescue_reminder")

RESCUE_REMIND_INTERVAL = 60
RESCUE_EXPIRY_MINUTES = 5


async def _reminder_tick(http_client_game, discord_client):
    """Single tick: find unresponded rescue requests that need a reminder and re-announce."""
    now = timezone.now()
    expiry_threshold = now - timedelta(minutes=RESCUE_EXPIRY_MINUTES)

    open_requests = [
        req
        async for req in RescueRequest.objects.filter(
            responders__isnull=True,
            timestamp__gte=expiry_threshold,
        )
        .select_related("character")
        .order_by("timestamp")
    ]

    remind_requests = []

    for req in open_requests:
        if req.last_reminded_at is None:
            remind_requests.append(req)
        elif (now - req.last_reminded_at).total_seconds() >= RESCUE_REMIND_INTERVAL:
            remind_requests.append(req)

    for req in remind_requests:
        minutes_waiting = int((now - req.timestamp).total_seconds() / 60)

        in_game_msg = _(
            "{name} still needs a rescue! Respond with /respond {request_id}"
        ).format(name=req.character.name, request_id=req.id)
        asyncio.create_task(announce(in_game_msg, http_client_game))

        if discord_client:
            try:
                from amc.utils import forward_to_discord

                discord_msg = _(
                    "@here **{name}** still needs rescue! ({minutes} min waiting)\n"
                    "Msg: {message}"
                ).format(
                    name=req.character.name,
                    minutes=minutes_waiting,
                    message=req.message or "",
                )
                await forward_to_discord(
                    discord_client,
                    settings.DISCORD_RESCUE_CHANNEL_ID,
                    discord_msg,
                    escape_mentions=False,
                    silent=True,
                )
            except Exception:
                logger.exception("Failed to send Discord reminder for rescue %s", req.id)

        req.last_reminded_at = now
        await req.asave(update_fields=["last_reminded_at"])

    if remind_requests:
        logger.info(f"Sent reminders for {len(remind_requests)} rescue request(s)")


async def run_rescue_reminder_loop(http_client_game, discord_client):
    """Long-running loop that re-announces unresponded rescue requests.

    Launched via asyncio.create_task from the Discord bot's setup_hook.
    """
    logger.info("Rescue reminder loop started")

    while True:
        try:
            await _reminder_tick(http_client_game, discord_client)
        except asyncio.CancelledError:
            logger.info("Rescue reminder loop shutting down")
            return
        except Exception:
            logger.exception("Rescue reminder tick tick error")

        await asyncio.sleep(RESCUE_REMIND_INTERVAL)
