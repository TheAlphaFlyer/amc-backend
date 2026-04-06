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
    """Compute the W-level (1–5) from remaining wanted heat."""
    if wanted_remaining <= 0:
        return 0
    return min(math.ceil(wanted_remaining / Wanted.LEVEL_PER_STAR), 5)


async def tick_wanted_countdown(http_client, http_client_mod) -> None:
    """Single tick of the wanted countdown. Called from an arq cron.

    Wanted status is permanent — it never decays automatically.
    Only police proximity causes wanted_remaining to decrease,
    using the inverse-square law (1/r²).
    """
    # Batch-load all active wanted records
    wanted_list = [
        w
        async for w in Wanted.objects.filter(
            expired_at__isnull=True,
            wanted_remaining__gt=0,
        ).select_related("character")
    ]
    if not wanted_list:
        return
    logger.info("wanted tick: %d active records", len(wanted_list))

    # Fetch player locations (best-effort; empty is fine)
    players = await get_players(http_client)
    locations = _build_player_locations(players) if players else {}

    # Identify on-duty police officers (only if we have locations)
    cop_locations = []
    if locations:
        online_threshold = timezone.now() - timedelta(seconds=60)
        police_sessions = [
            ps
            async for ps in PoliceSession.objects.filter(
                ended_at__isnull=True,
                character__last_online__gte=online_threshold,
            ).select_related("character")
        ]
        cop_guids = {
            ps.character.guid
            for ps in police_sessions
            if ps.character.guid and ps.character.guid in locations
        }
        for cg in cop_guids:
            _, cop_loc, _ = locations[cg]
            cop_locations.append(cop_loc)

    # No police on duty — nothing happens (wanted stays permanently)
    # Also nothing happens for offline suspects or suspects with no cops nearby

    expired_characters = []
    star_change_notifications = []  # (wanted, message) for deferred processing

    for wanted in wanted_list:
        sus_guid = wanted.character.guid
        old_stars = _compute_stars(wanted.wanted_remaining)

        # Offline suspect or no police at all → no decay
        if sus_guid not in locations or not cop_locations:
            continue

        # Online suspect with police on duty — apply 1/r² decay
        _, sus_loc, _ = locations[sus_guid]
        min_dist = min(_distance_3d(sus_loc, cop_loc) for cop_loc in cop_locations)

        # 1/r² decay: closer police = faster reduction
        clamped_dist = max(min_dist, Wanted.MIN_DISTANCE)
        decay_rate = min(
            Wanted.MAX_DECAY, (Wanted.REF_DISTANCE / clamped_dist) ** 2
        )
        decrement = TICK_INTERVAL * decay_rate

        if wanted.wanted_remaining > 0:
            wanted.wanted_remaining = max(0, wanted.wanted_remaining - decrement)
            if wanted.wanted_remaining <= 0:
                expired_characters.append(wanted.character)

        # Track star changes for deferred notification
        new_stars = _compute_stars(wanted.wanted_remaining)
        if new_stars != old_stars:
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
        logger.info(
            "wanted tick: %d records expired — %s",
            len(expired_ids),
            [c.name for c in expired_characters],
        )
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
                logger.warning(
                    f"Failed to send wanted star message to {wanted.character.name}"
                )
        try:
            await refresh_player_name(wanted.character, http_client_mod)
            refreshed_guids.add(sus_guid)
        except Exception:
            logger.warning(
                f"Failed to refresh name for {wanted.character.name} after star change"
            )

    # Refresh names and announce money secured for characters whose wanted just expired
    for char in expired_characters:
        _last_star_notified.pop(char.guid, None)
        if char.guid not in refreshed_guids:
            try:
                await refresh_player_name(char, http_client_mod)
            except Exception:
                logger.warning(
                    f"Failed to refresh name for {char.name} after wanted expired"
                )
        if char.guid:
            try:
                await announce_money_secured(char.guid, http_client)
            except Exception:
                logger.warning(f"Failed to announce money secured for {char.name}")
