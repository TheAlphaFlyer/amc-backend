"""Teleport and vehicle reset event handlers.

Handles: ServerResetVehicleAt, ServerTeleportCharacter,
ServerTeleportVehicle, ServerRespawnCharacter
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from amc.handlers import register
from amc.models import (
    Confiscation,
    Delivery,
    PoliceSession,
    ServerTeleportLog,
    Wanted,
)
from amc.mod_server import show_popup, transfer_money
from amc.game_server import announce

logger = logging.getLogger("amc.webhook.handlers.teleport")

TELEPORT_PENALTY_WINDOW = 10  # minutes — used for legacy lookup only
TELEPORT_PENALTY_ANNOUNCE_DELAY = 10  # seconds — debounce window for announcements
POLICE_TELEPORT_ARREST_COOLDOWN = 5  # minutes — cops can't arrest after teleporting


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


async def _handle_teleport_or_respawn(event, character, ctx):
    """Penalise criminals who teleport/reset within the confiscation window.

    Uses the same linear decay formula as police arrest confiscation:
    rate = max(0, 1 - elapsed_minutes / window). The penalty is deducted
    from the player's wallet and criminal_laundered_total is reversed.
    """
    timestamp = _parse_timestamp(event)

    # Log ALL teleports (including police) for audit
    hook_name = event.get("hook", "") if isinstance(event, dict) else ""
    await ServerTeleportLog.objects.acreate(
        timestamp=timestamp,
        player=character.player,
        character=character,
        hook=hook_name,
        data=event.get("data"),
    )

    # Skip police officers — they don't deliver Money
    is_police = await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aexists()
    if is_police:
        # Set cooldown to block this officer from arresting
        cooldown_key = f"police_teleport_cooldown:{character.guid}"
        await cache.aset(
            cooldown_key, True, timeout=POLICE_TELEPORT_ARREST_COOLDOWN * 60
        )
        return 0, 0, 0, 0

    # Find un-confiscated Money deliveries and compute penalty rate from Wanted status
    recent_deliveries = [
        d
        async for d in Delivery.objects.filter(
            character=character,
            cargo_key="Money",
            confiscations__isnull=True,
        )
    ]
    if not recent_deliveries:
        return 0, 0, 0, 0

    try:
        wanted = await Wanted.objects.aget(character=character)
        rate = max(0.0, wanted.wanted_remaining / Wanted.INITIAL_WANTED_SECONDS)
    except Wanted.DoesNotExist:
        rate = 0.0

    penalty = sum(round(d.payment * rate) for d in recent_deliveries)
    if penalty <= 0:
        return 0, 0, 0, 0

    # 1. Deduct from wallet
    if ctx.http_client_mod:
        await transfer_money(
            ctx.http_client_mod,
            int(-penalty),
            "Teleport Penalty",
            str(character.player.unique_id),
        )

    # 2. Reverse criminal_laundered_total (clamp to 0)
    await character.arefresh_from_db(fields=["criminal_laundered_total"])
    new_total = max(0, character.criminal_laundered_total - penalty)
    character.criminal_laundered_total = new_total
    await character.asave(update_fields=["criminal_laundered_total"])

    # 3. Record as Confiscation with officer=None (self-inflicted)
    conf = await Confiscation.objects.acreate(
        character=character,
        officer=None,
        cargo_key="Money",
        amount=penalty,
    )
    await conf.deliveries.aset([d.id for d in recent_deliveries])

    # 4. Clear Wanted status after penalty
    await Wanted.objects.filter(character=character).adelete()

    # 5. Refresh player name tag (criminal level may have dropped)
    from amc.player_tags import refresh_player_name
    await refresh_player_name(character, ctx.http_client_mod)

    # 6. Debounced popup + announcement
    cache_key = f"teleport_penalty:{character.guid}"
    prev_total = await cache.aget(cache_key, 0)
    new_total = prev_total + penalty
    await cache.aset(cache_key, new_total, timeout=30)
    if prev_total == 0:
        asyncio.create_task(
            _announce_teleport_penalty_after_delay(
                character.guid,
                character.name,
                str(character.player.unique_id),
                ctx.http_client,
                ctx.http_client_mod,
            )
        )

    return 0, 0, 0, 0


async def _announce_teleport_penalty_after_delay(
    character_guid,
    character_name,
    player_unique_id,
    http_client,
    http_client_mod,
    delay=TELEPORT_PENALTY_ANNOUNCE_DELAY,
):
    """Wait for the debounce window, then announce the accumulated teleport penalty."""
    await asyncio.sleep(delay)
    cache_key = f"teleport_penalty:{character_guid}"
    total = await cache.aget(cache_key, 0)
    await cache.adelete(cache_key)
    if total <= 0:
        return
    if http_client_mod:
        asyncio.create_task(
            show_popup(
                http_client_mod,
                f"You lost ${total:,} for teleporting during criminal cooldown.",
                character_guid=character_guid,
                player_id=player_unique_id,
            )
        )
    if http_client:
        await announce(
            f"{character_name} lost ${total:,} for teleporting during criminal cooldown",
            http_client,
            color="E74C3C",
        )


def _parse_timestamp(event):
    current_tz = timezone.get_current_timezone()
    return timezone.datetime.fromtimestamp(event["timestamp"], tz=current_tz)
