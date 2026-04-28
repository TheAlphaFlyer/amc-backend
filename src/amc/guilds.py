import logging

from django.utils import timezone

from amc.enums import VehicleKeyByLabel
from amc.models import (
    Character,
    GuildCharacter,
    GuildSession,
    GuildVehicle,
    Player,
)
from amc.mod_server import get_player_last_vehicle_parts, set_decal

logger = logging.getLogger("amc.guilds")


async def _find_matching_guild_vehicle(
    vehicle_name: str, character_guid: str, http_client_mod
) -> GuildVehicle | None:
    vehicle_key = VehicleKeyByLabel.get(vehicle_name)
    if not vehicle_key:
        return None

    candidates = [
        gv
        async for gv in GuildVehicle.objects.filter(vehicle_key=vehicle_key)
        .select_related("guild", "decal")
        .prefetch_related("parts")
    ]
    if not candidates:
        return None

    has_any_parts = any(gv.parts.all() for gv in candidates)
    if not has_any_parts:
        return candidates[0]

    try:
        parts_data = await get_player_last_vehicle_parts(
            http_client_mod, character_guid, complete=False
        )
    except Exception as e:
        logger.error(f"Failed to fetch vehicle parts for guild check ({character_guid}): {e}")
        return None

    player_part_keys = {p["Key"] for p in parts_data.get("parts", [])}

    fallback_no_parts = None
    for gv in candidates:
        required = list(gv.parts.all())
        if not required:
            if fallback_no_parts is None:
                fallback_no_parts = gv
            continue
        if all(rp.part_key in player_part_keys for rp in required):
            return gv

    return fallback_no_parts


async def handle_guild_session(
    character: Character,
    player: Player,
    http_client_mod,
    action: str,
    vehicle_name: str,
):
    try:
        if action == "EXITED":
            await _end_active_session(character)
            return

        guild_vehicle = await _find_matching_guild_vehicle(
            vehicle_name, str(character.guid), http_client_mod
        )

        if guild_vehicle:
            await _activate_guild(character, guild_vehicle, http_client_mod, str(player.unique_id))
        else:
            await _end_active_session(character)
    except Exception:
        logger.exception(f"Error handling guild session for {character.name}")


async def _end_active_session(character: Character):
    now = timezone.now()
    updated = await GuildSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aupdate(ended_at=now)
    if updated:
        logger.info(f"Ended guild session for {character.name}")


async def _activate_guild(
    character: Character,
    guild_vehicle: GuildVehicle,
    http_client_mod,
    player_id: str,
):
    now = timezone.now()
    guild = guild_vehicle.guild

    existing = await GuildSession.objects.filter(
        character=character, guild=guild, ended_at__isnull=True
    ).afirst()
    if existing:
        return

    await _end_active_session(character)

    await GuildSession.objects.acreate(
        guild=guild,
        character=character,
        started_at=now,
    )
    logger.info(f"Started guild session: {character.name} → {guild.abbreviation}")

    await GuildCharacter.objects.aget_or_create(
        guild=guild,
        character=character,
    )

    decal = guild_vehicle.decal
    if decal and decal.config:
        try:
            await set_decal(http_client_mod, player_id, decal.config)
        except Exception as e:
            logger.error(f"Failed to apply guild decal for {character.name}: {e}")
