"""Rescue reminder task for arq cron.

Periodically checks for unresponded rescue requests (no responders) and
re-announces them in-game every minute until someone responds or the request
expires (after RESCUE_EXPIRY_MINUTES).

Scheduled as an arq cron job in amc_backend.worker.WorkerSettings.
"""

import asyncio
import logging

from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext as _

from amc.models import RescueRequest
from amc.game_server import announce

logger = logging.getLogger("amc.rescue_reminder")

RESCUE_REMIND_INTERVAL = 60
RESCUE_EXPIRY_MINUTES = 5


async def send_rescue_reminders(ctx):
    """arq cron task: re-announce unresponded rescue requests in-game."""
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
        in_game_msg = _(
            "{name} still needs a rescue! Respond with /respond {request_id}"
        ).format(name=req.character.name, request_id=req.id)
        asyncio.create_task(announce(in_game_msg, ctx["http_client"]))

        req.last_reminded_at = now
        await req.asave(update_fields=["last_reminded_at"])

    if remind_requests:
        logger.info(f"Sent reminders for {len(remind_requests)} rescue request(s)")
