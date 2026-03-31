"""Auto-arrest patrol loop.

Continuously monitors on-duty police officers and nearby players.
When a suspect with recent Money deliveries is found within arrest range,
the system automatically executes an arrest (teleport to jail, confiscate,
announce) — matching the manual /arrest behavior.

The patrol loop runs as a long-lived asyncio task started from the Discord
bot's setup_hook, sharing the same http_client sessions.
"""

import asyncio
import logging

from datetime import timedelta

from django.core.cache import cache
from django.contrib.gis.geos import Point
from django.utils import timezone

from amc.commands.faction import (
    ARREST_CONFISCATION_WINDOW,
    SUSPECT_SPEED_LIMIT,
    _build_player_locations,
    _distance_3d,
    execute_arrest,
)
from amc.game_server import announce, get_players
from amc.mod_server import send_system_message
from amc.models import ArrestZone, Character, Delivery, PoliceSession

logger = logging.getLogger("amc.auto_arrest")

PATROL_POLL_INTERVAL = 0.5  # seconds between each poll cycle
AUTO_ARREST_STILL_TICKS = 5  # ticks the suspect must be still + in range (5 × 0.5s = 2.5s)
AUTO_ARREST_RADIUS_ON_FOOT = 1500   # 15m — cop on foot, checked only on first contact
AUTO_ARREST_RADIUS_IN_VEHICLE = 1000  # 10m — cop in vehicle, checked only on first contact


async def _has_recent_money_deliveries(character) -> bool:
    """Check if a character has un-confiscated Money deliveries within the window."""
    window_start = timezone.now() - timedelta(minutes=ARREST_CONFISCATION_WINDOW)
    return await Delivery.objects.filter(
        character=character,
        cargo_key="Money",
        timestamp__gte=window_start,
        confiscations__isnull=True,  # not yet confiscated
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
        arrest_radius = AUTO_ARREST_RADIUS_IN_VEHICLE if cop_has_vehicle else AUTO_ARREST_RADIUS_ON_FOOT

        # Zone check — cop must be inside an active ArrestZone
        if zones_exist:
            cop_point = Point(cop_loc[0], cop_loc[1], srid=3857)
            in_zone = await ArrestZone.objects.filter(
                active=True, polygon__contains=cop_point
            ).aexists()
            if not in_zone:
                continue

        # Teleport cooldown: skip cops who recently teleported
        tp_cooldown_key = f"police_teleport_cooldown:{cop_guid}"
        if await cache.aget(tp_cooldown_key):
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
                if speed_per_second > SUSPECT_SPEED_LIMIT:
                    # Moving too fast — reset counter
                    still_counters.pop(pair_key, None)
                    continue

            # Must have recent money deliveries
            sus_char = suspect_chars.get(sus_guid)
            if not sus_char:
                continue

            has_money = await _has_recent_money_deliveries(sus_char)
            if not has_money:
                continue

            # Increment stillness counter for this cop-suspect pair
            active_pairs.add(pair_key)
            still_counters[pair_key] = still_counters.get(pair_key, 0) + 1

            if still_counters[pair_key] >= AUTO_ARREST_STILL_TICKS:
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
    launched via asyncio.create_task from the Discord bot's setup_hook.
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
