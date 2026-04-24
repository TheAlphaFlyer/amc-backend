"""Police session management.

Similar to gov_employee.py — provides session-based police duty
instead of permanent faction membership.
"""

import math
from django.db.models import F
from django.utils import timezone
from amc.player_tags import refresh_player_name

POLICE_LEVEL_STEP = 50_000

# (name, x, y, z) — coordinates in game units
POLICE_STATIONS = [
    ("Jeju Police Station", -42361, -141792, -21094),
    ("Hallim Police Station", -325934, -2506, -21920),
    ("Seoguipo Police Station", -8776, 144044, -21084),
    ("Seongsan Police Station", 319727, -84041, -21921),
    ("Gapa Police Station", 77156, 648911, -9011),
    ("Gwangjin Police Station", 266983, 878250, -8911),
    ("Ara Police Station", 315281, 1335754, -19911),
]

# Risk premium: extra payout on Money deliveries per active online police officer.
SECURITY_BONUS_RATE = 0.50  # 50% per officer
SECURITY_BONUS_MAX = 2.5  # capped at 250% (max 5 police)


async def get_active_police_characters(exclude_character=None):
    """Return QuerySet of Character models for online police officers.

    An officer counts if they have an active PoliceSession (ended_at is null)
    AND their character.last_online is within the last 60 seconds.
    Optionally exclude a specific character (e.g. the arresting officer from
    a previous code path — though usually all officers are included).
    """
    from datetime import timedelta
    from amc.models import PoliceSession

    online_threshold = timezone.now() - timedelta(seconds=60)
    qs = (
        PoliceSession.objects.filter(
            ended_at__isnull=True,
            character__last_online__gte=online_threshold,
        )
        .select_related("character")
        .values_list("character", flat=True)
    )
    from amc.models import Character

    characters = Character.objects.filter(pk__in=qs)
    if exclude_character is not None:
        characters = characters.exclude(pk=exclude_character.pk)
    return characters


async def get_active_police_count() -> int:
    """Count police officers who are on duty AND currently online.

    An officer counts if they have an active PoliceSession (ended_at is null)
    AND their character.last_online is within the last 60 seconds.
    """
    from datetime import timedelta
    from amc.models import PoliceSession

    online_threshold = timezone.now() - timedelta(seconds=60)
    return await PoliceSession.objects.filter(
        ended_at__isnull=True,
        character__last_online__gte=online_threshold,
    ).acount()


def calculate_police_level(confiscated_total: int) -> int:
    """Calculate police level from cumulative confiscated amount.
    Level scales infinitely: floor(total / step) + 1"""
    return (confiscated_total // POLICE_LEVEL_STEP) + 1


async def is_police(character) -> bool:
    """Check if a character has an active police session."""
    from amc.models import PoliceSession

    return await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aexists()


def is_police_vehicle(vehicle_name: str | None) -> bool:
    """Check if a vehicle name indicates a police vehicle."""
    return bool(vehicle_name and "Police" in vehicle_name)


async def activate_police(character, session):
    """Start a new police session and refresh player tag."""
    from amc.models import PoliceSession

    # End any existing active session first
    await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aupdate(ended_at=timezone.now())

    await PoliceSession.objects.acreate(character=character)
    await refresh_player_name(character, session)


async def deactivate_police(character, session):
    """End the active police session and refresh player tag."""
    from amc.models import PoliceSession

    await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aupdate(ended_at=timezone.now())

    await refresh_player_name(character, session)


async def record_confiscation_for_level(
    character, amount, http_client=None, session=None
):
    """Increment confiscated total and check for level-up.

    Args:
        character: The officer's Character model.
        amount: Amount confiscated.
        http_client: HTTP client for announcements.
        session: HTTP client for mod server (name refresh).
    """
    old_level = calculate_police_level(character.police_confiscated_total)

    character.police_confiscated_total = F("police_confiscated_total") + int(amount)
    await character.asave(update_fields=["police_confiscated_total"])
    await character.arefresh_from_db(fields=["police_confiscated_total"])

    new_level = calculate_police_level(character.police_confiscated_total)
    if new_level != old_level:
        await refresh_player_name(character, session)

        if http_client:
            import asyncio
            from amc.game_server import announce

            asyncio.create_task(
                announce(
                    f"🎉 {character.name} has been promoted to Police Level {new_level}!",
                    http_client,
                    color="4A90D9",
                )
            )


# ---------------------------------------------------------------------------
# Police station suspect tick
# ---------------------------------------------------------------------------


def _game_units_to_metres(units: float) -> int:
    """Convert game units (100 = 1m) to metres."""
    return round(units / 100)


def _compass_direction(dx: float, dy: float) -> str:
    """Return compass direction from delta x/y (e.g. NE, SW)."""
    angle = math.atan2(dy, dx)
    if angle < 0:
        angle += 2 * math.pi
    dirs = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"]
    idx = int((angle + math.pi / 8) / (math.pi / 4)) % 8
    return dirs[idx]


async def tick_police_station_updates(http_client, http_client_mod) -> None:
    """Every 30s: send on-duty police a suspect list relative to their nearest station."""
    from datetime import timedelta
    from amc.commands.faction import _build_player_locations, _distance_3d
    from amc.game_server import get_players
    from amc.mod_server import send_system_message
    from amc.models import Wanted, PoliceSession

    # Active wanted records
    wanted_list = [
        w
        async for w in Wanted.objects.filter(
            expired_at__isnull=True,
            wanted_remaining__gt=0,
        ).select_related("character")
    ]
    if not wanted_list:
        return

    # Player locations
    players = await get_players(http_client)
    locations = _build_player_locations(players) if players else {}

    # Online suspects with known locations
    suspects = []
    for wanted in wanted_list:
        guid = wanted.character.guid
        if guid and guid in locations:
            _, loc, _ = locations[guid]
            suspects.append((wanted.character.name, loc))

    if not suspects:
        return

    # On-duty police officers
    online_threshold = timezone.now() - timedelta(seconds=60)
    police_sessions = [
        ps
        async for ps in PoliceSession.objects.filter(
            ended_at__isnull=True,
            character__last_online__gte=online_threshold,
        ).select_related("character")
    ]

    # Group officers by nearest station
    station_groups: dict[str, list] = {name: [] for name, *_ in POLICE_STATIONS}
    for ps in police_sessions:
        guid = ps.character.guid
        if not guid or guid not in locations:
            continue
        _, cop_loc, _ = locations[guid]

        nearest_name = None
        min_dist = float("inf")
        for name, sx, sy, sz in POLICE_STATIONS:
            dist = _distance_3d(cop_loc, (sx, sy, sz))
            if dist < min_dist:
                min_dist = dist
                nearest_name = name

        if nearest_name:
            station_groups[nearest_name].append(ps.character)

    # Send one message per station to its officers
    for name, sx, sy, sz in POLICE_STATIONS:
        officers = station_groups[name]
        if not officers:
            continue

        suspect_infos = []
        for char_name, loc in suspects:
            dist = _distance_3d(loc, (sx, sy, sz))
            dx = loc[0] - sx
            dy = loc[1] - sy
            direction = _compass_direction(dx, dy)
            suspect_infos.append((dist, char_name, direction))

        suspect_infos.sort(key=lambda x: x[0])
        lines = [
            f"{char_name} {_game_units_to_metres(dist)}m {direction}"
            for dist, char_name, direction in suspect_infos
        ]
        message = f"Suspects near {name}: " + ", ".join(lines)

        for officer in officers:
            try:
                await send_system_message(
                    http_client_mod, message, character_guid=officer.guid
                )
            except Exception:
                pass
