"""Police event handlers.

Handles: ServerArrivedAtPolicePatrolPoint, ServerSelectPolicePullOverPenaltyResponse,
ServerAddPolicePlayer, ServerRemovePolicePlayer, ServerPickupCargo
"""

from __future__ import annotations

import asyncio
import logging

from django.core.cache import cache

from amc.commands.faction import _build_player_locations, perform_arrest
from amc.handlers import register
from amc.special_cargo import ILLICIT_CARGO_KEYS
from amc.models import (
    Character,
    Confiscation,
    CriminalRecord,
    PolicePatrolLog,
    PolicePenaltyLog,
    PoliceSession,
    PoliceShiftLog,
)
from amc.mod_server import (
    despawn_player_cargo,
    send_system_message,
    transfer_money,
)
from amc.game_server import announce, get_players
from amc_finance.services import (
    record_treasury_confiscation_income,
    send_fund_to_player_wallet,
)

logger = logging.getLogger("amc.webhook.handlers.police")


# ---------------------------------------------------------------------------
# ServerArrivedAtPolicePatrolPoint
# ---------------------------------------------------------------------------


@register("ServerArrivedAtPolicePatrolPoint")
async def handle_patrol_arrived(event, player, character, ctx):
    patrol_point_id = event["data"].get("PatrolPointId", 0)
    base_payment = 0
    area_bonus_payment = 0

    if ctx.http_client_mod:
        from amc.mod_server import get_patrol_point_payments

        payments = await get_patrol_point_payments(ctx.http_client_mod)
        if patrol_point_id in payments:
            base_payment = payments[patrol_point_id]["BasePayment"]
            area_bonus_payment = payments[patrol_point_id]["AreaBonusPayment"]

    timestamp = _parse_timestamp(event)
    await PolicePatrolLog.objects.acreate(
        timestamp=timestamp,
        player=player,
        patrol_point_id=patrol_point_id,
        base_payment=base_payment,
        area_bonus_payment=area_bonus_payment,
        data=event.get("data"),
    )
    return 0, 0, 0, 0


# ---------------------------------------------------------------------------
# ServerSelectPolicePullOverPenaltyResponse
# ---------------------------------------------------------------------------


@register("ServerSelectPolicePullOverPenaltyResponse")
async def handle_police_penalty(event, player, character, ctx):
    timestamp = _parse_timestamp(event)
    warning_only = event["data"].get("bWarningOnly", False)
    await PolicePenaltyLog.objects.acreate(
        timestamp=timestamp,
        player=player,
        warning_only=warning_only,
        data=event.get("data"),
    )

    if warning_only:
        return 0, 0, 0, 0

    # Auto-arrest suspects with an active criminal record during pull-over
    suspect_data = event["data"].get("SuspectCharacter", {})
    suspect_guid = suspect_data.get("CharacterGuid")
    if not suspect_guid:
        return 0, 0, 0, 0

    try:
        suspect_character = await Character.objects.select_related("player").aget(
            guid=suspect_guid
        )
    except Character.DoesNotExist:
        return 0, 0, 0, 0

    has_record = await CriminalRecord.objects.filter(
        character=suspect_character,
        cleared_at__isnull=True,
    ).aexists()
    if not has_record:
        return 0, 0, 0, 0

    # Suspect is wanted — execute arrest
    players = await get_players(ctx.http_client)
    if not players:
        return 0, 0, 0, 0

    locations = _build_player_locations(players)
    if suspect_guid not in locations:
        return 0, 0, 0, 0

    targets = {suspect_guid: locations[suspect_guid]}
    target_chars = {suspect_guid: suspect_character}

    try:
        await perform_arrest(
            officer_character=character,
            targets=targets,
            target_chars=target_chars,
            http_client=ctx.http_client,
            http_client_mod=ctx.http_client_mod,
            officer_message_format="{names} arrested and sent to jail.",
        )
    except ValueError as e:
        logger.warning("Pull-over arrest skipped: %s", e)

    return 0, 0, 0, 0


# ---------------------------------------------------------------------------
# ServerAddPolicePlayer / ServerRemovePolicePlayer
# ---------------------------------------------------------------------------


@register("ServerAddPolicePlayer")
async def handle_police_shift_start(event, player, character, ctx):
    await _create_police_shift(event, player, PoliceShiftLog.Action.START)
    return 0, 0, 0, 0


@register("ServerRemovePolicePlayer")
async def handle_police_shift_end(event, player, character, ctx):
    await _create_police_shift(event, player, PoliceShiftLog.Action.END)
    return 0, 0, 0, 0


async def _create_police_shift(event, player, action):
    timestamp = _parse_timestamp(event)
    await PoliceShiftLog.objects.acreate(
        timestamp=timestamp,
        player=player,
        action=action,
        data=event.get("data"),
    )


# ---------------------------------------------------------------------------
# ServerPickupCargo — confiscation
# ---------------------------------------------------------------------------

CONFISCATION_ANNOUNCE_DELAY = 30  # seconds


@register("ServerPickupCargo")
async def handle_pickup_cargo(event, player, character, ctx):
    """Handle ServerPickupCargo: confiscate illicit cargo if picker is police."""
    cargo = event["data"].get("Cargo", {})
    cargo_key = cargo.get("Net_CargoKey")
    if cargo_key not in ILLICIT_CARGO_KEYS:
        return 0, 0, 0, 0

    # Must be active police (on duty)
    is_police = await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aexists()
    if not is_police:
        return 0, 0, 0, 0

    # Despawn cargo regardless of confiscation validity
    try:
        await despawn_player_cargo(ctx.http_client_mod, str(character.guid))
    except Exception:
        logger.warning("Failed to despawn money cargo for police %s", character.guid)

    payment = cargo.get("Net_Payment", 0)
    previous_owner_guid = cargo.get("PreviousOwnerCharacterGuid")
    if not previous_owner_guid or payment <= 0:
        return 0, 0, 0, 0

    # No self-confiscation
    if str(character.guid).upper() == previous_owner_guid.upper():
        return 0, 0, 0, 0

    # Look up previous owner
    previous_owner = await (
        Character.objects.select_related("player")
        .filter(guid=previous_owner_guid)
        .afirst()
    )

    is_prev_police = False
    if previous_owner:
        is_prev_police = await PoliceSession.objects.filter(
            character=previous_owner, ended_at__isnull=True
        ).aexists()
    if is_prev_police:
        return 0, 0, 0, 0

    # 1. Record confiscation
    await Confiscation.objects.acreate(
        character=previous_owner,
        officer=character,
        cargo_key=cargo_key,
        amount=payment,
    )

    # 2. Charge previous owner
    if previous_owner:
        await transfer_money(
            ctx.http_client_mod,
            int(-payment),
            "Money Confiscated",
            str(previous_owner.player.unique_id),
        )

    # 3. Credit treasury
    await record_treasury_confiscation_income(payment, "Police Confiscation")

    # 4. Debounced announcement
    if ctx.http_client:
        cache_key = f"money_confiscated:{character.guid}"
        prev_total = await cache.aget(cache_key, 0)
        if prev_total == 0:
            await cache.aset(cache_key, payment, timeout=60)
            asyncio.create_task(
                _announce_confiscation_after_delay(
                    character.guid, ctx.http_client, delay=CONFISCATION_ANNOUNCE_DELAY
                )
            )
        else:
            await cache.aset(cache_key, prev_total + payment, timeout=60)

    # 5. Track confiscation for police level
    from amc.police import record_confiscation_for_level

    await record_confiscation_for_level(
        character, payment, http_client=ctx.http_client, session=ctx.http_client_mod
    )

    # 6. Reward officer with confiscated amount
    if ctx.http_client_mod:
        await transfer_money(
            ctx.http_client_mod,
            int(payment),
            "Confiscation Reward",
            str(character.player.unique_id),
        )
        await send_fund_to_player_wallet(payment, character, "Confiscation Reward")
        await send_system_message(
            ctx.http_client_mod,
            f"You earned ${payment:,} confiscation reward.",
            character_guid=character.guid,
        )

    return 0, 0, 0, 0


async def _announce_confiscation_after_delay(character_guid, http_client, delay=30):
    """Wait for the debounce window, then announce the accumulated confiscation total."""
    await asyncio.sleep(delay)
    cache_key = f"money_confiscated:{character_guid}"
    total = await cache.aget(cache_key, 0)
    await cache.adelete(cache_key)
    if total > 0:
        await announce(
            f"${total:,} has been confiscated by police",
            http_client,
            color="4A90D9",
        )


def _parse_timestamp(event):
    from amc.handlers.utils import parse_event_timestamp

    return parse_event_timestamp(event)
