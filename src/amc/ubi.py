import asyncio
from decimal import Decimal
from datetime import timedelta
from django.utils import timezone
from amc.models import (
    Character,
    CharacterLocation,
)
from amc.game_server import get_players
from amc.mod_server import transfer_money
from amc_finance.services import send_fund_to_player_wallet

TASK_FREQUENCY = 20  # minutes
ACTIVE_GRANT_AMOUNT = 18_000 / (60 / TASK_FREQUENCY)
AFK_GRANT_AMOUNT = 6_000 / (60 / TASK_FREQUENCY)
MAX_LEVEL = 400


async def handout_ubi(ctx):
    http_client = ctx.get("http_client")
    http_client_mod = ctx.get("http_client_mod")
    now = timezone.now()
    start_time = now - timedelta(minutes=TASK_FREQUENCY)

    players = await get_players(http_client)
    if not players:
        return

    # Batch fetch all characters in one query
    guids = [p["character_guid"] for _, p in players]
    characters = {
        c.guid: c
        async for c in Character.objects.filter(
            guid__in=guids,
            driver_level__isnull=False,
            reject_ubi=False,
        )
    }

    # Filter out characters with no driver_level (driver_level=0 is falsy)
    eligible = {guid: c for guid, c in characters.items() if c.driver_level}

    if not eligible:
        return

    # Batch activity check: single query for all characters
    activity = await CharacterLocation.batch_get_character_activity(
        list(eligible.values()),
        start_time,
        now,
    )

    # Process payouts sequentially (transactional money operations)
    for player_id, player in players:
        guid = player["character_guid"]
        character = eligible.get(guid)
        if not character:
            continue

        try:
            is_online, is_active = activity.get(character.id, (False, False))

            if is_active:
                grant_amount = ACTIVE_GRANT_AMOUNT
            else:
                grant_amount = AFK_GRANT_AMOUNT
            amount = min(
                Decimal(str(grant_amount)),
                character.driver_level
                * Decimal(str(grant_amount))
                * Decimal(str(character.ubi_multiplier))
                / MAX_LEVEL,
            )

            await send_fund_to_player_wallet(
                amount, character, "Universal Basic Income"
            )
            await transfer_money(
                http_client_mod, int(amount), "Universal Basic Income", player_id
            )
            await asyncio.sleep(1)
        except Exception as e:
            print(f"Error handing out UBI to player {player_id}: {e}")
            continue
