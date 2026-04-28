import logging

from django.utils import timezone

from amc.enums import VehicleKeyByLabel, VehiclePartSlot
from amc.models import Character, Guild, GuildCharacter, GuildSession, Player
from amc.mod_server import get_player_last_vehicle_parts, set_decal

logger = logging.getLogger("amc.guilds")


async def _find_matching_guild_with_engine(
    vehicle_name: str, character_guid: str, http_client_mod
) -> Guild | None:
    vehicle_key = VehicleKeyByLabel.get(vehicle_name)
    if not vehicle_key:
        return None

    candidates = [
        g async for g in Guild.objects.filter(vehicle_key=vehicle_key).select_related("decal")
    ]
    if not candidates:
        return None

    needs_engine_check = [g for g in candidates if g.engine_part_key is not None]
    if not needs_engine_check:
        return candidates[0]

    try:
        parts_data = await get_player_last_vehicle_parts(
            http_client_mod, character_guid, complete=False
        )
    except Exception as e:
        logger.error(f"Failed to fetch vehicle parts for guild check ({character_guid}): {e}")
        return None

    parts = parts_data.get("parts", [])
    engine_key = None
    for part in parts:
        if part.get("Slot") == VehiclePartSlot.Engine.value:
            engine_key = part.get("Key")
            break

    if engine_key is None:
        no_engine = [g for g in candidates if g.engine_part_key is None]
        return no_engine[0] if no_engine else None

    for g in candidates:
        if g.engine_part_key == engine_key:
            return g

    no_engine = [g for g in candidates if g.engine_part_key is None]
    return no_engine[0] if no_engine else None


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

        guild = await _find_matching_guild_with_engine(
            vehicle_name, str(character.guid), http_client_mod
        )

        if guild:
            await _activate_guild(character, guild, http_client_mod, str(player.unique_id))
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
    guild: Guild,
    http_client_mod,
    player_id: str,
):
    now = timezone.now()

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

    if guild.decal and guild.decal.config:
        try:
            await set_decal(http_client_mod, player_id, guild.decal.config)
        except Exception as e:
            logger.error(f"Failed to apply guild decal for {character.name}: {e}")
