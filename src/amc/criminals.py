import logging
import math
import time

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

# Escape gate constants
ESCAPE_DISTANCE = 20_000  # 200m (game units) — suspect must be beyond all cops to clear
ESCAPE_FLOOR = 0.1        # minimum wanted_remaining while near police (cannot expire)
ESCAPE_MSG_COOLDOWN = 30  # seconds between "escape the police" popup messages

# Bounty growth — amount ($) added per second while police are nearby.
# Uses the same 1/r² proximity factor as heat decay (higher factor = nearer).
# At REF_DISTANCE (50m) growth = BOUNTY_GROWTH_PER_TICK * 1.0 = $200/s.
# At point blank (10m) factor = MAX_DECAY (10) so growth = $2,000/s.
BOUNTY_GROWTH_PER_TICK = 200  # $/s at reference distance (50m)

# Logout heat escalation — same 1/r² law as teleport, but capped lower since
# logging out near police is less deliberate than teleporting.
LOGOUT_HEAT_MAX = 300     # max heat added when police are point-blank
LOGOUT_PROXIMITY_RANGE = 200_000  # 2km in game units — no effect beyond this

# Tracks the last notified star level per character guid
_last_star_notified: dict[str, int] = {}

# Tracks when the last escape popup was sent per character guid (monotonic clock)
_last_escape_msg_sent: dict[str, float] = {}


def _calculate_logout_heat(min_police_distance: float) -> float:
    """Heat added when logging out near police (1/r² law, same as teleport).

    - Point blank (10m):  300 heat (max)
    - 50m:                ~12 heat
    - 100m+:              ~3 heat
    - >2km:               0 (not called)
    """
    clamped_dist = max(min_police_distance, Wanted.MIN_DISTANCE)
    proximity_factor = min(
        Wanted.MAX_DECAY, (Wanted.REF_DISTANCE / clamped_dist) ** 2
    )
    return (proximity_factor / Wanted.MAX_DECAY) * LOGOUT_HEAT_MAX


async def escalate_heat_on_logout(character, http_client) -> None:
    """Escalate wanted heat when a Wanted player logs out near police.

    Uses the same 1/r² formula as the teleport penalty.  No police within
    2km → no effect.  The Wanted record is never expired here — the
    player must be arrested or escape through the normal gate.
    """
    wanted = await Wanted.objects.filter(
        character=character,
        expired_at__isnull=True,
        wanted_remaining__gt=0,
    ).afirst()
    if not wanted:
        return

    # Need the player's last known location.  If the game has already removed
    # them from the player list we fall back to the cached last_location.
    players = await get_players(http_client)
    locations = _build_player_locations(players) if players else {}

    sus_guid = wanted.character.guid
    if sus_guid not in locations:
        # Player already gone from the server — use last_location if available
        if not character.last_location:
            logger.debug("escalate_heat_on_logout: no location for %s", character.name)
            return
        sus_loc = (character.last_location.x, character.last_location.y, character.last_location.z)
    else:
        _, sus_loc, _ = locations[sus_guid]

    # Find on-duty police
    online_threshold = timezone.now() - timedelta(seconds=60)
    police_sessions = [
        ps
        async for ps in PoliceSession.objects.filter(
            ended_at__isnull=True,
            character__last_online__gte=online_threshold,
        ).select_related("character")
    ]
    cop_locations = [
        locations[ps.character.guid][1]
        for ps in police_sessions
        if ps.character.guid and ps.character.guid in locations
    ]
    if not cop_locations:
        logger.debug("escalate_heat_on_logout: no police online for %s", character.name)
        return

    min_dist = min(_distance_3d(sus_loc, cop_loc) for cop_loc in cop_locations)
    if min_dist > LOGOUT_PROXIMITY_RANGE:
        logger.debug(
            "escalate_heat_on_logout: %s too far from police (%.0f > %.0f)",
            character.name, min_dist, LOGOUT_PROXIMITY_RANGE,
        )
        return

    heat = _calculate_logout_heat(min_dist)
    old_remaining = wanted.wanted_remaining
    old_stars = min(math.ceil(old_remaining / Wanted.LEVEL_PER_STAR), 5)

    max_heat = Wanted.INITIAL_WANTED_LEVEL * 5
    wanted.wanted_remaining = min(max_heat, wanted.wanted_remaining + heat)
    await wanted.asave(update_fields=["wanted_remaining"])

    new_stars = min(math.ceil(wanted.wanted_remaining / Wanted.LEVEL_PER_STAR), 5)
    logger.info(
        "logout heat: %s — dist=%.0f heat=%.1f W%d→W%d",
        character.name, min_dist, heat, old_stars, new_stars,
    )

STAR_MESSAGES = {
    5: "You are wanted. Police are closing in!",
    4: "Your wanted status is decreasing. 4 stars remaining.",
    3: "Your wanted status is decreasing. 3 stars remaining.",
    2: "Your wanted status is decreasing. 2 stars remaining.",
    1: "Your wanted status is almost over. Escape the police to clear it!",
    0: "Your wanted status has expired.",
}

ESCAPE_MESSAGE = "Escape the police to clear your wanted status!"


def compute_stars(wanted_remaining: float) -> int:
    """Compute the star level (1–5) from remaining wanted heat."""
    if wanted_remaining <= 0:
        return 0
    return min(math.ceil(wanted_remaining / Wanted.LEVEL_PER_STAR), 5)


# Internal alias kept for use within this module
_compute_stars = compute_stars


async def tick_wanted_countdown(http_client, http_client_mod) -> None:
    """Single tick of the wanted countdown. Called from an arq cron.

    Wanted status only decays when police are physically nearby (1/r² law).
    No cops online or suspect offline → no decay, wanted persists.

    Escape gate: wanted_remaining decays near police but is clamped at
    ESCAPE_FLOOR. The suspect must flee beyond ESCAPE_DISTANCE from all cops
    for the final clearing to occur.
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

    expired_characters = []
    star_change_notifications = []  # (wanted, message) for deferred processing
    escape_popups = []              # guids to send escape popup to

    for wanted in wanted_list:
        sus_guid = wanted.character.guid
        old_stars = _compute_stars(wanted.wanted_remaining)

        # Offline suspect or no cops online → no decay, wanted persists
        if sus_guid not in locations or not cop_locations:
            continue

        _, sus_loc, _ = locations[sus_guid]
        min_dist = min(_distance_3d(sus_loc, cop_loc) for cop_loc in cop_locations)

        if min_dist >= ESCAPE_DISTANCE:
            # --- Suspect has escaped police proximity ---
            # Only clear if wanted has been brought to the floor (by prior proximity decay)
            if wanted.wanted_remaining <= ESCAPE_FLOOR:
                wanted.wanted_remaining = 0
                expired_characters.append(wanted.character)
            # If still above the floor, no decay happens — suspect must return
            # near police first, let it decay to the floor, then escape.
        else:
            # --- Suspect is near police (< ESCAPE_DISTANCE) ---
            # Apply 1/r² decay, but clamp at ESCAPE_FLOOR — cannot expire here.
            clamped_dist = max(min_dist, Wanted.MIN_DISTANCE)
            proximity_factor = min(
                Wanted.MAX_DECAY, (Wanted.REF_DISTANCE / clamped_dist) ** 2
            )
            decrement = TICK_INTERVAL * proximity_factor

            if wanted.wanted_remaining > ESCAPE_FLOOR:
                wanted.wanted_remaining = max(ESCAPE_FLOOR, wanted.wanted_remaining - decrement)

            # Bounty grows proportionally to police proximity (same 1/r² factor)
            wanted.amount += int(BOUNTY_GROWTH_PER_TICK * proximity_factor)

            # At floor near police → queue throttled escape popup
            if wanted.wanted_remaining <= ESCAPE_FLOOR:
                now = time.monotonic()
                last_sent = _last_escape_msg_sent.get(sus_guid, 0.0)
                if now - last_sent >= ESCAPE_MSG_COOLDOWN:
                    _last_escape_msg_sent[sus_guid] = now
                    escape_popups.append(sus_guid)

        # Track star changes for deferred notification
        new_stars = _compute_stars(wanted.wanted_remaining)
        if new_stars != old_stars:
            last_notified = _last_star_notified.get(sus_guid)
            if last_notified is None or new_stars != last_notified:
                _last_star_notified[sus_guid] = new_stars
                msg = STAR_MESSAGES.get(new_stars)
                star_change_notifications.append((wanted, msg))

    # Bulk save — must happen BEFORE refresh_player_name so it reads correct DB state
    await Wanted.objects.abulk_update(wanted_list, ["wanted_remaining", "amount"])

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

    # Send escape popups (throttled)
    for sus_guid in escape_popups:
        try:
            await send_system_message(
                http_client_mod,
                ESCAPE_MESSAGE,
                character_guid=sus_guid,
            )
        except Exception:
            logger.warning("Failed to send escape popup to %s", sus_guid)

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
        _last_escape_msg_sent.pop(char.guid, None)
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
