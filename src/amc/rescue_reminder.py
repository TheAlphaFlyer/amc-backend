"""Rescue reminder task for arq cron.

Periodically checks for unresponded rescue requests (no responders) and
re-announces them on Discord and in-game every minute until someone responds
or the request expires (after RESCUE_EXPIRY_MINUTES).

Scheduled as an arq cron job in amc_backend.worker.WorkerSettings.
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


async def send_rescue_reminders(ctx):
    """arq cron task: re-announce unresponded rescue requests."""
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
        asyncio.create_task(announce(in_game_msg, ctx["http_client"]))

        discord_client = ctx.get("discord_client")
        if discord_client and discord_client.is_ready():
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

                async def send_discord():
                    await forward_to_discord(
                        discord_client,
                        settings.DISCORD_RESCUE_CHANNEL_ID,
                        discord_msg,
                        escape_mentions=False,
                        silent=True,
                    )

                asyncio.run_coroutine_threadsafe(send_discord(), discord_client.loop)
            except Exception:
                logger.exception("Failed to send Discord reminder for rescue %s", req.id)

        req.last_reminded_at = now
        await req.asave(update_fields=["last_reminded_at"])

    if remind_requests:
        logger.info(f"Sent reminders for {len(remind_requests)} rescue request(s)")
