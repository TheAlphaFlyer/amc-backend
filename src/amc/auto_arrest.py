"""Auto-arrest patrol loop.

Continuously monitors on-duty police officers and nearby players.
When a suspect with an active Wanted record is found within arrest range,
the system automatically executes an arrest (teleport to jail, confiscate,
announce) — matching the manual /arrest behavior.

The patrol loop runs as a long-lived asyncio task started from the arq
worker's startup, sharing the same http_client sessions.
"""

import asyncio
import logging

from datetime import timedelta

from django.utils import timezone

from amc.commands.faction import (
    _build_player_locations,
    _distance_3d,
    execute_arrest,
)
from amc.game_server import announce, get_players
from amc.mod_server import send_system_message
from amc.models import ArrestZone, Character, PoliceSession, Wanted
from amc.police import is_police_vehicle

logger = logging.getLogger("amc.auto_arrest")

# ── Tuning constants (human-readable) ────────────────────────────────
PATROL_POLL_INTERVAL = 0.5   # seconds between each poll cycle
AUTO_ARREST_DURATION = 3.0   # seconds suspect must be still + in range
AUTO_ARREST_WARNING_AT = 1.0 # seconds into tracking before warning the suspect
AUTO_ARREST_RADIUS_ON_FOOT_M = 30   # metres — cop arrest range on foot
AUTO_ARREST_RADIUS_IN_VEHICLE_M = 20  # metres — cop arrest range in vehicle
AUTO_ARREST_SPEED_LIMIT_KMPH = 40   # km/h — suspects faster than this escape

# ── Derived constants (game: 100 units = 1 metre) ────────────────────
_UNITS_PER_METRE = 100
_STILL_TICKS = round(AUTO_ARREST_DURATION / PATROL_POLL_INTERVAL)
_WARNING_TICK = round(AUTO_ARREST_WARNING_AT / PATROL_POLL_INTERVAL)
_RADIUS_ON_FOOT = AUTO_ARREST_RADIUS_ON_FOOT_M * _UNITS_PER_METRE
_RADIUS_IN_VEHICLE = AUTO_ARREST_RADIUS_IN_VEHICLE_M * _UNITS_PER_METRE
_SPEED_LIMIT = AUTO_ARREST_SPEED_LIMIT_KMPH * _UNITS_PER_METRE / 3.6  # units/second


async def _is_wanted(character) -> bool:
    """Check if a character has an active Wanted status with wanted_remaining > 0 and no expired_at."""
    return await Wanted.objects.filter(
        character=character,
        wanted_remaining__gt=0,
        expired_at__isnull=True,
    ).aexists()


async def _patrol_tick(http_client, http_client_mod, prev_locations, still_counters=None):
    """Single patrol tick: poll players, find police, auto-arrest nearby suspects.

    Args:
        http_client: Game server HTTP client for announcements.
        http_client_mod: Mod server HTTP client for teleport/money/messages.
        prev_locations: dict of guid -> (unique_id, (x,y,z), has_vehicle) from
            the previous tick, used for suspect speed checks.
        still_counters: dict of (cop_guid, suspect_guid) -> int counting
            consecutive ticks the suspect has been still & in range of this cop.

    Returns:
        (current_locations, still_counters) tuple.
    """
    if still_counters is None:
        still_counters = {}
    players = await get_players(http_client)
    if not players:
        return prev_locations, still_counters

    locations = _build_player_locations(players)
    if not locations:
        return {}, {}

    # Build vehicle name lookup from raw player data
    vehicle_names: dict[str, str | None] = {}
    for _uid, pdata in players:
        guid = pdata.get("character_guid")
        if guid:
            vehicle = pdata.get("vehicle")
            if isinstance(vehicle, dict):
                vehicle_names[guid] = vehicle.get("name")
            else:
                vehicle_names[guid] = vehicle if vehicle else None

    # Identify on-duty police officers
    online_threshold = timezone.now() - timedelta(seconds=60)
    police_sessions = [
        ps async for ps in PoliceSession.objects.filter(
            ended_at__isnull=True,
            character__last_online__gte=online_threshold,
        ).select_related("character", "character__player")
    ]
    if not police_sessions:
        return locations, still_counters

    cop_guids = {ps.character.guid for ps in police_sessions if ps.character.guid in locations}
    if not cop_guids:
        return locations, still_counters

    # Build cop character lookup
    cop_chars = {ps.character.guid: ps.character for ps in police_sessions}

    # Identify all non-police player guids
    all_guids = set(locations.keys())
    suspect_guids = all_guids - cop_guids

    if not suspect_guids:
        return locations, still_counters

    # Batch-load suspect Characters (need player_id for money transfer)
    suspect_chars = {}
    async for char in Character.objects.filter(
        guid__in=suspect_guids
    ).select_related("player"):
        suspect_chars[char.guid] = char

    # Check ArrestZone enforcement
    zones_exist = await ArrestZone.objects.filter(active=True).aexists()

    # Track which (cop, suspect) pairs are still valid this tick
    active_pairs = set()

    # For each cop, find nearby suspects eligible for auto-arrest
    for cop_guid in cop_guids:
        cop_uid, cop_loc, cop_has_vehicle = locations[cop_guid]

        # Only allow auto-arrest while on foot or in a police vehicle
        if cop_has_vehicle:
            if not is_police_vehicle(vehicle_names.get(cop_guid)):
                continue
        arrest_radius = _RADIUS_IN_VEHICLE if cop_has_vehicle else _RADIUS_ON_FOOT

        # Zone check — cop must be inside an active ArrestZone
        if zones_exist:
            from django.contrib.gis.geos import Point
            cop_point = Point(cop_loc[0], cop_loc[1], srid=3857)
            in_zone = await ArrestZone.objects.filter(
                active=True, polygon__contains=cop_point
            ).aexists()
            if not in_zone:
                continue

        cop_char = cop_chars.get(cop_guid)
        if not cop_char:
            continue

        # Find suspects within arrest radius
        arrestable_targets = {}
        arrestable_chars = {}

        for sus_guid in suspect_guids:
            if sus_guid not in locations:
                continue

            sus_uid, sus_loc, sus_has_vehicle = locations[sus_guid]
            pair_key = (cop_guid, sus_guid)
            already_tracking = pair_key in still_counters

            # Radius check: only on first contact (not yet tracking)
            if not already_tracking:
                dist = _distance_3d(cop_loc, sus_loc)
                if dist > arrest_radius:
                    continue

            # Speed check: normalize to units/second for consistent behavior
            if sus_guid in prev_locations:
                prev_uid, prev_loc, _ = prev_locations[sus_guid]
                distance_moved = _distance_3d(prev_loc, sus_loc)
                speed_per_second = distance_moved / PATROL_POLL_INTERVAL
                if speed_per_second > _SPEED_LIMIT:
                    # Moving too fast — reset counter and notify cop if tracking was active
                    was_tracking = still_counters.pop(pair_key, 0) > 0
                    if was_tracking:
                        sus_name = suspect_chars.get(sus_guid)
                        asyncio.create_task(
                            send_system_message(
                                http_client_mod,
                                f"{sus_name.name if sus_name else 'Suspect'} is moving too fast to arrest.",
                                character_guid=cop_guid,
                            )
                        )
                    continue

            # Must have active Wanted status
            sus_char = suspect_chars.get(sus_guid)
            if not sus_char:
                continue

            if not await _is_wanted(sus_char):
                continue

            # Increment stillness counter for this cop-suspect pair
            active_pairs.add(pair_key)
            prev_count = still_counters.get(pair_key, 0)
            still_counters[pair_key] = prev_count + 1

            # Warn suspect when tracking reaches the warning threshold
            if prev_count < _WARNING_TICK <= still_counters[pair_key]:
                asyncio.create_task(
                    send_system_message(
                        http_client_mod,
                        "\u26a0\ufe0f A police officer is attempting to arrest you! Flee now!",
                        character_guid=sus_guid,
                    )
                )

            if still_counters[pair_key] >= _STILL_TICKS:
                arrestable_targets[sus_guid] = (sus_uid, sus_loc, sus_has_vehicle)
                arrestable_chars[sus_guid] = sus_char

        if not arrestable_targets:
            continue

        # Execute arrest
        try:
            arrested_names, total_confiscated = await execute_arrest(
                officer_character=cop_char,
                targets=arrestable_targets,
                target_chars=arrestable_chars,
                http_client=http_client,
                http_client_mod=http_client_mod,
            )
        except ValueError as e:
            logger.warning(f"Auto-arrest skipped: {e}")
            return locations, still_counters

        if arrested_names:
            names_arrested = ", ".join(arrested_names)

            # System message to officer
            await send_system_message(
                http_client_mod,
                f"{names_arrested} auto-arrested and sent to jail.",
                character_guid=cop_char.guid,
            )

            # Server announcement
            if total_confiscated > 0:
                asyncio.create_task(
                    announce(
                        f"{names_arrested} arrested by {cop_char.name}! ${total_confiscated:,} confiscated.",
                        http_client,
                    )
                )
            else:
                asyncio.create_task(
                    announce(
                        f"{names_arrested} arrested by {cop_char.name}!",
                        http_client,
                    )
                )

            # Remove arrested suspects from this tick's pool and reset counters
            for g in arrestable_targets:
                suspect_guids.discard(g)
                # Clear all still_counters involving this suspect
                for key in list(still_counters):
                    if key[1] == g:
                        del still_counters[key]

    # Prune stale counters for pairs no longer active
    for key in list(still_counters):
        if key not in active_pairs:
            del still_counters[key]

    return locations, still_counters


async def run_patrol_loop(http_client, http_client_mod):
    """Long-running patrol loop that auto-arrests suspects near police.

    This function never returns under normal operation. It is meant to be
    launched via asyncio.create_task from the arq worker's startup.
    """
    logger.info("Auto-arrest patrol loop started")
    prev_locations = {}
    still_counters = {}

    while True:
        try:
            prev_locations, still_counters = await _patrol_tick(
                http_client, http_client_mod, prev_locations, still_counters
            )
        except asyncio.CancelledError:
            logger.info("Auto-arrest patrol loop shutting down")
            return
        except Exception:
            logger.exception("Auto-arrest patrol tick error")

        await asyncio.sleep(PATROL_POLL_INTERVAL)
