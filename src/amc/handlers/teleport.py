"""Teleport and vehicle reset event handlers.

Handles: ServerResetVehicleAt, ServerTeleportCharacter,
ServerTeleportVehicle, ServerRespawnCharacter
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import timedelta

from django.utils import timezone

from amc.handlers import register
from amc.models import (
    PoliceSession,
    ServerTeleportLog,
    Wanted,
)
from amc.mod_server import send_system_message
from amc.game_server import announce, get_players
from amc.commands.faction import _build_player_locations, _distance_3d

logger = logging.getLogger("amc.webhook.handlers.teleport")

# Teleport heat escalation: proximity-only (no base)
TELEPORT_HEAT_MAX = 300  # max heat added when police are point-blank
TELEPORT_PROXIMITY_RANGE = 200_000  # 2km in game units — no effect beyond this


# ---------------------------------------------------------------------------
# ServerResetVehicleAt
# ---------------------------------------------------------------------------


@register("ServerResetVehicleAt")
async def handle_reset_vehicle(event, player, character, ctx):
    timestamp = _parse_timestamp(event)
    if ctx.is_rp_mode and character.last_login < timestamp - timedelta(seconds=15):
        asyncio.create_task(
            announce(
                f"{character.name}'s vehicle has been despawned for using roadside recovery while on RP mode",
                ctx.http_client,
                color="FFA500",
            )
        )
    return 0, 0, 0, 0


# ---------------------------------------------------------------------------
# ServerTeleportCharacter / ServerTeleportVehicle / ServerRespawnCharacter
# ---------------------------------------------------------------------------


@register("ServerTeleportCharacter")
async def _handle_teleport_character(event, player, character, ctx):
    return await _handle_teleport_or_respawn(event, character, ctx)


@register("ServerTeleportVehicle")
async def _handle_teleport_vehicle(event, player, character, ctx):
    return await _handle_teleport_or_respawn(event, character, ctx)


@register("ServerRespawnCharacter")
async def _handle_respawn_character(event, player, character, ctx):
    return await _handle_teleport_or_respawn(event, character, ctx)


def _calculate_proximity_heat(min_police_distance: float) -> float:
    """Calculate heat from police proximity using inverse-square law.

    Only called when police are within TELEPORT_PROXIMITY_RANGE (2km).
    Closer police → more heat, disincentivising teleport to escape arrest.
    - Point blank (10m):  ~300 heat (MAX)
    - 50m:                ~12 heat
    - 100m+:              ~3 heat
    - >2km:               not called (0)
    """
    clamped_dist = max(min_police_distance, Wanted.MIN_DISTANCE)
    proximity_factor = min(
        Wanted.MAX_DECAY, (Wanted.REF_DISTANCE / clamped_dist) ** 2
    )
    return (proximity_factor / Wanted.MAX_DECAY) * TELEPORT_HEAT_MAX


async def _get_nearest_police_distance(
    character_guid: str, http_client
) -> float | None:
    """Find the distance to the nearest on-duty police officer.

    Returns None if the suspect is offline or no police are on duty.
    """
    players = await get_players(http_client)
    if not players:
        return None

    locations = _build_player_locations(players)
    if character_guid not in locations:
        return None

    # Find on-duty police
    online_threshold = timezone.now() - timedelta(seconds=60)
    police_sessions = [
        ps
        async for ps in PoliceSession.objects.filter(
            ended_at__isnull=True,
            character__last_online__gte=online_threshold,
        ).select_related("character")
    ]

    cop_locations = []
    for ps in police_sessions:
        cop_guid = ps.character.guid
        if cop_guid and cop_guid in locations:
            _, cop_loc, _ = locations[cop_guid]
            cop_locations.append(cop_loc)

    if not cop_locations:
        return None

    _, sus_loc, _ = locations[character_guid]
    return min(_distance_3d(sus_loc, cop_loc) for cop_loc in cop_locations)


async def _handle_teleport_or_respawn(event, character, ctx):
    """Escalate wanted level when a wanted player teleports near police.

    Teleporting increases wanted_remaining based on police proximity (1/r²).
    Only applies if there is a police officer within 2km.
    No effect if no police are nearby.

    This disincentivises players from teleporting to escape an imminent arrest.
    """
    timestamp = _parse_timestamp(event)

    # Log ALL teleports for audit
    hook_name = event.get("hook", "") if isinstance(event, dict) else ""
    await ServerTeleportLog.objects.acreate(
        timestamp=timestamp,
        player=character.player,
        character=character,
        hook=hook_name,
        data=event.get("data"),
    )

    # Only escalate for actively wanted players
    try:
        wanted = await Wanted.objects.aget(
            character=character, expired_at__isnull=True, wanted_remaining__gt=0
        )
    except Wanted.DoesNotExist:
        return 0, 0, 0, 0

    # Only escalate if police are within 2km
    min_distance = await _get_nearest_police_distance(
        character.guid, ctx.http_client
    )
    if min_distance is None or min_distance > TELEPORT_PROXIMITY_RANGE:
        return 0, 0, 0, 0

    heat_increase = _calculate_proximity_heat(min_distance)
    logger.info(
        "teleport heat: %s — dist=%.0f heat=%.1f",
        character.name,
        min_distance,
        heat_increase,
    )

    # Apply heat increase (cap at INITIAL_WANTED_LEVEL * 5 to bound growth)
    old_remaining = wanted.wanted_remaining
    old_stars = min(math.ceil(old_remaining / Wanted.LEVEL_PER_STAR), 5)

    max_heat = Wanted.INITIAL_WANTED_LEVEL * 5
    wanted.wanted_remaining = min(max_heat, wanted.wanted_remaining + heat_increase)
    await wanted.asave(update_fields=["wanted_remaining"])

    new_stars = min(math.ceil(wanted.wanted_remaining / Wanted.LEVEL_PER_STAR), 5)

    # Refresh player name tag if star level changed
    if new_stars != old_stars:
        from amc.player_tags import refresh_player_name

        await refresh_player_name(character, ctx.http_client_mod)

    # Notify the player
    if ctx.http_client_mod:
        msg = (
            f"Teleporting near police increased your wanted level! "
            f"W{old_stars}→W{new_stars}"
        )
        asyncio.create_task(
            send_system_message(
                ctx.http_client_mod,
                msg,
                character_guid=character.guid,
            )
        )

    return 0, 0, 0, 0


def _parse_timestamp(event):
    from amc.handlers.utils import parse_event_timestamp

    return parse_event_timestamp(event)
