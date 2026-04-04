"""Discord notification helpers for webhook events.

Extracted from webhook.py.
"""

from __future__ import annotations

import asyncio


async def post_discord_delivery_embed(
    discord_client,
    character,
    cargo_key,
    quantity,
    delivery_source,
    delivery_destination,
    payment,
    subsidy,
    vehicle_key,
    job=None,
    delivery_id=None,
):
    jobs_cog = discord_client.get_cog("JobsCog")
    delivery_source_name = ""
    delivery_destination_name = ""
    if delivery_source:
        delivery_source_name = delivery_source.name
    if delivery_destination:
        delivery_destination_name = delivery_destination.name

    if jobs_cog and hasattr(jobs_cog, "post_delivery_embed"):
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None,
            lambda: asyncio.run_coroutine_threadsafe(
                jobs_cog.post_delivery_embed(
                    character.name,
                    cargo_key,
                    quantity,
                    delivery_source_name,
                    delivery_destination_name,
                    payment,
                    subsidy,
                    vehicle_key,
                    job=job,
                    delivery_id=delivery_id,
                ),
                discord_client.loop,
            ),
        )
