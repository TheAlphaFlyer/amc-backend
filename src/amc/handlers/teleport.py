"""Teleport and vehicle reset event handlers.

Handles: ServerResetVehicleAt, ServerTeleportCharacter,
ServerTeleportVehicle, ServerRespawnCharacter
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from amc.handlers import register
from amc.models import (
    PoliceSession,
    ServerTeleportLog,
    Wanted,
)
from amc.game_server import announce
from amc.mod_server import despawn_player_vehicle

logger = logging.getLogger("amc.webhook.handlers.teleport")


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
        if ctx.http_client_mod:
            asyncio.create_task(
                despawn_player_vehicle(
                    ctx.http_client_mod,
                    str(character.guid),
                    category="current",
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



async def _handle_teleport_or_respawn(event, character, ctx):
    """Send wanted criminals to jail when they teleport via the game.

    Fires on ServerTeleportCharacter / ServerTeleportVehicle /
    ServerRespawnCharacter.

    For wanted players the outcome is:
      - Teleport to jail (+ activate jail boundary via jailed_until).
      - Show a popup explaining why.
      - Wanted record is left active — police must still make a proper arrest.

    For non-wanted players, the event is a no-op (just logged).
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

    # Only act on actively wanted players
    try:
        wanted = await Wanted.objects.aget(
            character=character, expired_at__isnull=True, wanted_remaining__gt=0
        )
    except Wanted.DoesNotExist:
        return 0, 0, 0, 0

    # Skip police officers
    is_police = await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aexists()
    if is_police:
        return 0, 0, 0, 0

    logger.info(
        "Wanted criminal %s triggered %s — sending to jail",
        character.name,
        hook_name,
    )

    from amc.commands.teleport import _auto_arrest_wanted_criminal

    await _auto_arrest_wanted_criminal(
        wanted,
        character,
        character.player,
        ctx.http_client_mod,
    )

    return 0, 0, 0, 0


def _parse_timestamp(event):
    from amc.handlers.utils import parse_event_timestamp

    return parse_event_timestamp(event)
