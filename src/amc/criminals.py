"""Criminal wanted system.

Proximity-based countdown for Wanted suspects. Closer police slow the
countdown, giving officers more time to arrest. Distant or absent police
let it tick down at full speed. Runs as an arq cron every second.
"""

import logging

from datetime import timedelta

from django.utils import timezone

from amc.commands.faction import _build_player_locations, _distance_3d
from amc.game_server import get_players
from amc.models import PoliceSession, Wanted

logger = logging.getLogger("amc.criminals")

TICK_INTERVAL = 1.0  # seconds between ticks (matches cron cadence)
PROXIMITY_REF_DISTANCE = 20000  # 200m — at or beyond this distance, countdown runs at full speed
MIN_SLOWDOWN = 0.05  # 5% — minimum countdown speed (at very close range)


async def tick_wanted_countdown(http_client) -> None:
    """Single tick of the wanted countdown. Called from an arq cron."""
    players = await get_players(http_client)
    if not players:
        return

    locations = _build_player_locations(players)
    if not locations:
        return

    # Identify on-duty police officers
    online_threshold = timezone.now() - timedelta(seconds=60)
    police_sessions = [
        ps async for ps in PoliceSession.objects.filter(
            ended_at__isnull=True,
            character__last_online__gte=online_threshold,
        ).select_related("character")
    ]

    cop_guids = {ps.character.guid for ps in police_sessions if ps.character.guid in locations}

    # Build cop location list
    cop_locations = []
    for cg in cop_guids:
        if cg in locations:
            _, cop_loc, _ = locations[cg]
            cop_locations.append(cop_loc)

    # Batch-load all active wanted records
    wanted_list = [
        w async for w in Wanted.objects.filter(
            wanted_remaining__gt=0,
        ).select_related("character")
    ]
    if not wanted_list:
        return

    for wanted in wanted_list:
        sus_guid = wanted.character.guid
        if sus_guid not in locations:
            # Offline suspect — still tick at full speed
            wanted.wanted_remaining = max(0, wanted.wanted_remaining - TICK_INTERVAL)
            continue

        if not cop_locations:
            # No cops to compute proximity — full-speed decrement
            wanted.wanted_remaining = max(0, wanted.wanted_remaining - TICK_INTERVAL)
        else:
            _, sus_loc, _ = locations[sus_guid]
            min_dist = min(_distance_3d(sus_loc, cop_loc) for cop_loc in cop_locations)
            slowdown = max(MIN_SLOWDOWN, min_dist / PROXIMITY_REF_DISTANCE)
            decrement = TICK_INTERVAL * slowdown
            wanted.wanted_remaining = max(0, wanted.wanted_remaining - decrement)

    # Bulk save
    await Wanted.objects.abulk_update(wanted_list, ["wanted_remaining"])

    # Delete expired
    await Wanted.objects.filter(wanted_remaining__lte=0).adelete()
