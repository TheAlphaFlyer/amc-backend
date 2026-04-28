import logging

from django.utils import timezone

from amc.enums import VehicleKey
from amc.models import (
    Character,
    GuildCharacter,
    GuildSession,
    GuildVehicle,
    Player,
)
from amc.mod_server import get_player_last_vehicle_parts, set_decal
from amc.player_tags import refresh_player_name

logger = logging.getLogger("amc.guilds")


async def _find_matching_guild_vehicle(
    vehicle_name: str, character_guid: str, http_client_mod
) -> GuildVehicle | None:
    # vehicle_name from game logs is already the VehicleKey value (e.g. "Trophy2")
    if vehicle_name not in VehicleKey.values:
        return None

    candidates = [
        gv
        async for gv in GuildVehicle.objects.filter(vehicle_key=vehicle_name)
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
            await _end_active_session(character, http_client_mod)
            return

        guild_vehicle = await _find_matching_guild_vehicle(
            vehicle_name, str(character.guid), http_client_mod
        )

        if guild_vehicle:
            await _activate_guild(character, guild_vehicle, http_client_mod, str(player.unique_id))
        else:
            await _end_active_session(character, http_client_mod)
    except Exception:
        logger.exception(f"Error handling guild session for {character.name}")


async def _end_active_session(character: Character, http_client_mod=None):
    now = timezone.now()
    updated = await GuildSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aupdate(ended_at=now)
    if updated:
        logger.info(f"Ended guild session for {character.name}")
        if http_client_mod:
            await refresh_player_name(character, http_client_mod)


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

    await _end_active_session(character, http_client_mod)

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

    await refresh_player_name(character, http_client_mod)

    decal = guild_vehicle.decal
    if decal and decal.config:
        try:
            await set_decal(http_client_mod, player_id, decal.config)
        except Exception as e:
            logger.error(f"Failed to apply guild decal for {character.name}: {e}")


async def check_guild_cargo(
    character, cargo_key, payment, damage
) -> tuple[GuildSession | None, int]:
    session = await (
        GuildSession.objects.filter(character=character, ended_at__isnull=True)
        .select_related("guild__cargo_requirement")
        .afirst()
    )
    if not session:
        return None, 0

    try:
        req = session.guild.cargo_requirement
    except Exception:
        return None, 0

    if req.allowed_cargo_keys and cargo_key not in req.allowed_cargo_keys:
        return None, 0
    if req.excluded_cargo_keys and cargo_key in req.excluded_cargo_keys:
        return None, 0
    if req.max_damage is not None and damage > req.max_damage:
        return None, 0
    if req.min_payment is not None and payment < req.min_payment:
        return None, 0
    if req.max_payment is not None and payment > req.max_payment:
        return None, 0

    bonus = int(payment * req.bonus_pct / 100)
    return session, bonus


async def check_guild_passenger(
    character, passenger_type, comfort, urgent, limo, offroad, comfort_rating, payment
) -> tuple[GuildSession | None, int]:
    session = await (
        GuildSession.objects.filter(character=character, ended_at__isnull=True)
        .select_related("guild__passenger_requirement")
        .afirst()
    )
    if not session:
        return None, 0

    try:
        req = session.guild.passenger_requirement
    except Exception:
        return None, 0

    if req.allowed_passenger_types and passenger_type not in req.allowed_passenger_types:
        return None, 0
    if req.require_comfort is not None and comfort != req.require_comfort:
        return None, 0
    if req.require_urgent is not None and urgent != req.require_urgent:
        return None, 0
    if req.require_limo is not None and limo != req.require_limo:
        return None, 0
    if req.require_offroad is not None and offroad != req.require_offroad:
        return None, 0
    if comfort and req.min_comfort_rating is not None and comfort_rating < req.min_comfort_rating:
        return None, 0
    if comfort and req.max_comfort_rating is not None and comfort_rating > req.max_comfort_rating:
        return None, 0

    bonus = int(payment * req.bonus_pct / 100)
    return session, bonus
