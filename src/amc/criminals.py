import logging
import math

from datetime import timedelta

from django.utils import timezone

from amc.commands.faction import _build_player_locations, _distance_3d
from amc.game_server import get_players
from amc.models import PoliceSession, Wanted
from amc.mod_server import send_system_message
from amc.player_tags import refresh_player_name
from amc.special_cargo import announce_money_secured

logger = logging.getLogger("amc.criminals")

TICK_INTERVAL = 1.0  # seconds between ticks (matches cron cadence)
PROXIMITY_REF_DISTANCE = 20000  # 200m — at or beyond this distance, countdown runs at full speed
MIN_SLOWDOWN = 0.05  # 5% — minimum countdown speed (at very close range)

# Tracks the last notified star level per character guid
_last_star_notified: dict[str, int] = {}

STAR_MESSAGES = {
    5: "You are wanted. Police are closing in!",
    4: "Your wanted status is decreasing. 4 stars remaining.",
    3: "Your wanted status is decreasing. 3 stars remaining.",
    2: "Your wanted status is decreasing. 2 stars remaining.",
    1: "Your wanted status is almost over. 1 star remaining.",
    0: "Your wanted status has expired.",
}


def _compute_stars(wanted_remaining: float) -> int:
    """Compute the star display count from remaining wanted seconds."""
    if wanted_remaining <= 0:
        return 0
    return min(math.ceil(wanted_remaining / 60) + 1, 5)


async def tick_wanted_countdown(http_client, http_client_mod) -> None:
    """Single tick of the wanted countdown. Called from an arq cron."""
    # Batch-load all active wanted records first — must always run
    wanted_list = [
        w async for w in Wanted.objects.filter(
            wanted_remaining__gt=0,
        ).select_related("character")
    ]
    if not wanted_list:
        return

    # Fetch player locations (best-effort; empty is fine)
    players = await get_players(http_client)
    locations = _build_player_locations(players) if players else {}

    # Identify on-duty police officers (only if we have locations)
    cop_locations = []
    if locations:
        online_threshold = timezone.now() - timedelta(seconds=60)
        police_sessions = [
            ps async for ps in PoliceSession.objects.filter(
                ended_at__isnull=True,
                character__last_online__gte=online_threshold,
            ).select_related("character")
        ]
        cop_guids = {ps.character.guid for ps in police_sessions if ps.character.guid and ps.character.guid in locations}
        for cg in cop_guids:
            _, cop_loc, _ = locations[cg]
            cop_locations.append(cop_loc)

    # No police on duty — immediately expire all wanted records
    if not cop_locations:
        # Notify online suspects before expiry
        for wanted in wanted_list:
            if wanted.character.guid in locations:
                try:
                    await send_system_message(
                        http_client_mod,
                        STAR_MESSAGES[0],
                        character_guid=wanted.character.guid,
                    )
                except Exception:
                    logger.warning(f"Failed to send wanted expired message to {wanted.character.name}")
        # Bulk expire all
        await Wanted.objects.filter(id__in=[w.id for w in wanted_list]).aupdate(
            wanted_remaining=0,
            expired_at=timezone.now(),
        )
        # Refresh names and announce money secured
        for char in (w.character for w in wanted_list):
            _last_star_notified.pop(char.guid, None)
            try:
                await refresh_player_name(char, http_client_mod)
            except Exception:
                logger.warning(f"Failed to refresh name for {char.name} after wanted expired")
            if char.guid:
                try:
                    await announce_money_secured(char.guid, http_client)
                except Exception:
                    logger.warning(f"Failed to announce money secured for {char.name}")
        return

    # Cops are on duty — tick countdown per suspect
    expired_characters = []
    star_change_notifications = []  # (wanted, message) for deferred processing

    for wanted in wanted_list:
        sus_guid = wanted.character.guid
        old_stars = _compute_stars(wanted.wanted_remaining)

        if sus_guid not in locations:
            # Offline suspect — tick at full speed
            if wanted.wanted_remaining > 0:
                wanted.wanted_remaining = max(0, wanted.wanted_remaining - TICK_INTERVAL)
                if wanted.wanted_remaining <= 0:
                    expired_characters.append(wanted.character)
            continue

        _, sus_loc, _ = locations[sus_guid]
        min_dist = min(_distance_3d(sus_loc, cop_loc) for cop_loc in cop_locations)
        slowdown = max(MIN_SLOWDOWN, min(1.0, min_dist / PROXIMITY_REF_DISTANCE))
        decrement = TICK_INTERVAL * slowdown
        if wanted.wanted_remaining > 0:
            wanted.wanted_remaining = max(0, wanted.wanted_remaining - decrement)
            if wanted.wanted_remaining <= 0:
                expired_characters.append(wanted.character)

        # Track star changes for deferred notification (online suspects only)
        new_stars = _compute_stars(wanted.wanted_remaining)
        if new_stars != old_stars and sus_guid in locations:
            last_notified = _last_star_notified.get(sus_guid)
            if last_notified is None or new_stars != last_notified:
                _last_star_notified[sus_guid] = new_stars
                msg = STAR_MESSAGES.get(new_stars)
                star_change_notifications.append((wanted, msg))

    # Bulk save — must happen BEFORE refresh_player_name so it reads correct DB state
    await Wanted.objects.abulk_update(wanted_list, ["wanted_remaining"])

    # Mark expired (set expired_at instead of deleting)
    expired_ids = [w.id for w in wanted_list if w.wanted_remaining <= 0]
    if expired_ids:
        await Wanted.objects.filter(id__in=expired_ids).aupdate(
            wanted_remaining=0,
            expired_at=timezone.now(),
        )

    # Send star-change messages and refresh names (DB is now up-to-date)
    refreshed_guids = set()
    for wanted, msg in star_change_notifications:
        sus_guid = wanted.character.guid
        if msg:
            try:
                await send_system_message(
                    http_client_mod,
                    msg,
                    character_guid=sus_guid,
                )
            except Exception:
                logger.warning(f"Failed to send wanted star message to {wanted.character.name}")
        try:
            await refresh_player_name(wanted.character, http_client_mod)
            refreshed_guids.add(sus_guid)
        except Exception:
            logger.warning(f"Failed to refresh name for {wanted.character.name} after star change")

    # Refresh names and announce money secured for characters whose wanted just expired
    for char in expired_characters:
        _last_star_notified.pop(char.guid, None)
        if char.guid not in refreshed_guids:
            try:
                await refresh_player_name(char, http_client_mod)
            except Exception:
                logger.warning(f"Failed to refresh name for {char.name} after wanted expired")
        if char.guid:
            try:
                await announce_money_secured(char.guid, http_client)
            except Exception:
                logger.warning(f"Failed to announce money secured for {char.name}")

