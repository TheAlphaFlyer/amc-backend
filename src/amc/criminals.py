import asyncio
import logging
import math
import time

from datetime import timedelta

from django.db.models import F
from django.utils import timezone

from amc.commands.faction import _build_player_locations, _distance_3d, execute_arrest
from amc.game_server import get_players
from amc.models import CriminalRecord, PoliceSession, Wanted
from amc.mod_detection import POLICE_DUTY_WHITELIST
from amc.mod_server import make_suspect, send_system_message
from amc.player_tags import refresh_player_name
from amc.special_cargo import announce_money_secured, WANTED_MIN_BOUNTY

logger = logging.getLogger("amc.criminals")

TICK_INTERVAL = 1.0  # seconds between ticks (matches cron cadence)

# Escape gate constants
ESCAPE_DISTANCE = 50_000  # 500m (game units) — suspect must be beyond all cops to clear
ESCAPE_FLOOR = 0.1        # minimum wanted_remaining while near police (cannot expire)
ESCAPE_MSG_COOLDOWN = 30  # seconds between "escape the police" popup messages

# Underwater auto-arrest threshold (game units)
UNDERWATER_Z_THRESHOLD = -22455

# Time-based decay — online suspects always decay; clears in BASE_WANTED_DURATION seconds.
BASE_WANTED_DURATION = 300  # 5 minutes
BASE_DECAY_PER_TICK = Wanted.INITIAL_WANTED_LEVEL / BASE_WANTED_DURATION  # 1.0/tick

# Police proximity SLOWS decay (1/r² law).
# effective_decay = BASE_DECAY_PER_TICK / (1 + proximity_factor)
# At no police (factor=0):      1.0/tick  (clears in 5 min)
# At REF_DISTANCE (100m, f=1):  0.5/tick  (clears in 10 min)
# At MIN_DISTANCE  (10m,  f=10): ≈0.09/tick (clears in ~55 min)
# Escape gate ensures it cannot expire while within ESCAPE_DISTANCE regardless.

# Bounty growth — amount ($) added per second while police are nearby (within ESCAPE_DISTANCE).
# Uses 1/r proximity factor (flatter than decay's 1/r²), capped at 1.0 ($100/s).
# At 200m (factor=0.5): growth = $50/s → ~$15k over 5 min chase.
# At REF_DISTANCE (100m, factor=1.0): growth = $100/s (cap).
BOUNTY_GROWTH_PER_TICK = 100  # $/s at reference distance (100m), rate is capped at this

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


async def escalate_heat_on_logout(character, http_client, http_client_mod=None) -> None:
    """Auto-arrest when a Wanted player logs out near police.

    If the player is within LOGOUT_PROXIMITY_RANGE of any on-duty police officer,
    treats the logout as an arrest: expires Wanted, confiscates bounty + delivery
    earnings, clears CriminalRecord, and marks the character for jailing on next
    login.  If no police are nearby or the player is too far, falls back to the
    original heat escalation behaviour.
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

    # Player logged out within range of police — treat as arrest
    if http_client_mod:
        from amc.commands.faction import execute_arrest

        await character.arefresh_from_db(fields=["player"])
        guid = character.guid or str(character.pk)
        targets = {guid: (str(character.player.unique_id), sus_loc, False)}
        target_chars = {guid: character}

        try:
            arrested_names, total_confiscated = await execute_arrest(
                officer_character=None,
                targets=targets,
                target_chars=target_chars,
                http_client=http_client,
                http_client_mod=http_client_mod,
            )
            logger.info(
                "logout arrest: %s — dist=%.0f confiscated=$%d",
                character.name, min_dist, total_confiscated,
            )
            return
        except ValueError as exc:
            logger.warning("logout arrest failed (jail not configured?): %s", exc)
        except Exception:
            logger.exception(
                "logout arrest failed unexpectedly for %s", character.name
            )

    # Fallback: escalate heat if execute_arrest is unavailable or failed
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


async def create_or_refresh_wanted(
    character,
    http_client_mod,
    *,
    amount: int = 0,
    wanted_remaining: int = 600,
    set_by=None,
) -> tuple[Wanted, bool]:
    """Create or refresh a Wanted record for the given character.

    Returns a tuple of (active Wanted instance, created) where *created*
    is True when a brand-new record was inserted.
    Called by cargo handlers for all illicit cargo types and by police commands.

    Args:
        character: The Character model instance.
        http_client_mod: Mod server HTTP client.
        amount: Additional bounty to accumulate on the Wanted record.
            Typically 0 — bounty grows from police proximity in tick_wanted_countdown.
            Values are floored at WANTED_MIN_BOUNTY.
        wanted_remaining: Initial wanted_remaining value for new or reset records.
            Defaults to 600 seconds (10 minutes).
        set_by: The Character model instance of the police officer who set
            this wanted status (police commands only).
    """

    # Enforce minimum bounty per event.
    effective_amount = max(amount, WANTED_MIN_BOUNTY)
    initial_wanted = wanted_remaining

    created = False
    active_wanted = await Wanted.objects.filter(
        character=character,
        expired_at__isnull=True,
    ).afirst()
    if active_wanted:
        active_wanted.wanted_remaining = initial_wanted
        active_wanted.amount = F("amount") + effective_amount
        await active_wanted.asave(update_fields=["wanted_remaining", "amount"])
        await active_wanted.arefresh_from_db(fields=["amount"])
    else:
        active_wanted = await Wanted.objects.acreate(
            character=character,
            wanted_remaining=initial_wanted,
            amount=effective_amount,
            set_by=set_by,
        )
        created = True

    await refresh_player_name(character, http_client_mod)
    asyncio.create_task(
        send_system_message(
            http_client_mod,
            "You are wanted. Police are closing in!",
            character_guid=character.guid,
        )
    )

    # Set the player as a suspect in-game so police can chase them
    if http_client_mod and character.guid:
        try:
            await make_suspect(http_client_mod, character.guid)
        except Exception:
            logger.warning(
                "make_suspect failed for %s (guid=%s)",
                character.name, character.guid, exc_info=True,
            )

    return active_wanted, created


def compute_stars(wanted_remaining: float) -> int:
    """Compute the star level (1–5) from remaining wanted heat."""
    if wanted_remaining <= 0:
        return 0
    return min(math.ceil(wanted_remaining / Wanted.LEVEL_PER_STAR), 5)


# Internal alias kept for use within this module
_compute_stars = compute_stars


async def tick_wanted_countdown(http_client, http_client_mod) -> None:
    """Single tick of the wanted countdown. Called from an arq cron.

    Time-based decay: online suspects always lose BASE_DECAY_PER_TICK per tick,
    clearing in BASE_WANTED_DURATION (5 min) with no police nearby.

    Police proximity SLOWS decay via 1/r² law:
        effective_decay = BASE_DECAY_PER_TICK / (1 + proximity_factor)
    Closer police → larger factor → slower decay. Decay never reverses.

    Bounty growth: uses 1/r law (flatter than decay) while police are within
    ESCAPE_DISTANCE (500m).  $50/s at 200m, ~$15k over a 5-min chase.

    Escape gate: cannot expire (clamped at ESCAPE_FLOOR) while any officer
    is within ESCAPE_DISTANCE (500m). Beyond 500m, full base-rate decay resumes.

    Offline suspects: no decay, wanted persists indefinitely.
    """
    # Batch-load all active wanted records
    wanted_list = [
        w
        async for w in Wanted.objects.filter(
            expired_at__isnull=True,
            wanted_remaining__gt=0,
        ).select_related("character__player")
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
        ).select_related("character__player")
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

        # Offline suspect → no decay, wanted persists
        if sus_guid not in locations:
            continue

        _, sus_loc, _ = locations[sus_guid]

        # Underwater suspects are automatically arrested
        if sus_loc[2] < UNDERWATER_Z_THRESHOLD:
            if http_client_mod:
                targets = {
                    sus_guid: (
                        str(wanted.character.player.unique_id),
                        sus_loc,
                        False,
                    )
                }
                target_chars = {sus_guid: wanted.character}
                try:
                    arrested_names, total_confiscated = await execute_arrest(
                        officer_character=None,
                        targets=targets,
                        target_chars=target_chars,
                        http_client=http_client,
                        http_client_mod=http_client_mod,
                    )
                    logger.info(
                        "underwater arrest: %s — z=%.0f confiscated=$%d",
                        wanted.character.name,
                        sus_loc[2],
                        total_confiscated,
                    )
                except ValueError as exc:
                    logger.warning(
                        "underwater arrest failed (jail not configured?): %s", exc
                    )
                except Exception:
                    logger.exception(
                        "underwater arrest failed unexpectedly for %s",
                        wanted.character.name,
                    )
            _last_star_notified.pop(sus_guid, None)
            _last_escape_msg_sent.pop(sus_guid, None)
            continue

        # Default: full base decay rate
        effective_decay = BASE_DECAY_PER_TICK * TICK_INTERVAL
        near_police = False

        if cop_locations:
            min_dist = min(_distance_3d(sus_loc, cop_loc) for cop_loc in cop_locations)

            if min_dist < ESCAPE_DISTANCE:
                near_police = True
                clamped_dist = max(min_dist, Wanted.MIN_DISTANCE)
                proximity_factor = min(
                    Wanted.MAX_DECAY, (Wanted.REF_DISTANCE / clamped_dist) ** 2
                )
                # Proximity slows decay: divide by (1 + factor)
                effective_decay = (BASE_DECAY_PER_TICK / (1 + proximity_factor)) * TICK_INTERVAL

                # Bounty grows proportionally to police proximity (1/r, capped at $500/s)
                bounty_factor = min(1.0, Wanted.REF_DISTANCE / clamped_dist)
                wanted.amount += int(BOUNTY_GROWTH_PER_TICK * bounty_factor)

        # Apply decay
        if near_police:
            # Cannot expire while within ESCAPE_DISTANCE — clamp at floor
            if wanted.wanted_remaining > ESCAPE_FLOOR:
                wanted.wanted_remaining = max(ESCAPE_FLOOR, wanted.wanted_remaining - effective_decay)
            # At floor near police → queue throttled escape popup
            if wanted.wanted_remaining <= ESCAPE_FLOOR:
                now = time.monotonic()
                last_sent = _last_escape_msg_sent.get(sus_guid, 0.0)
                if now - last_sent >= ESCAPE_MSG_COOLDOWN:
                    _last_escape_msg_sent[sus_guid] = now
                    escape_popups.append(sus_guid)
        else:
            # No cops within escape distance — full decay, can expire freely
            wanted.wanted_remaining = max(0.0, wanted.wanted_remaining - effective_decay)
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


# ---------------------------------------------------------------------------
# Criminal Record decay
# ---------------------------------------------------------------------------

CRIMINAL_RECORD_HALF_LIFE_MINUTES = 120  # 2 hours of online time
CRIMINAL_RECORD_DECAY_FACTOR = 0.5 ** (1 / CRIMINAL_RECORD_HALF_LIFE_MINUTES)
CRIMINAL_RECORD_DECAY_FLOOR = 100  # confiscatable amounts below this are zeroed out
ONLINE_THRESHOLD_SECONDS = 60  # character considered online if last_online < 60s ago


async def refresh_suspect_tags(http_client_mod) -> None:
    """Re-apply the suspect flag to every online wanted player.

    Called every 10 seconds via arq cron, decoupled from the 1-second wanted
    countdown tick so that suspect-tagging cadence can be tuned independently.
    """
    wanted_list = [
        w
        async for w in Wanted.objects.filter(
            expired_at__isnull=True,
            wanted_remaining__gt=0,
        ).select_related("character")
    ]
    if not wanted_list:
        return

    players = await get_players(http_client_mod)
    locations = _build_player_locations(players) if players else {}

    for wanted in wanted_list:
        sus_guid = wanted.character.guid
        if not sus_guid or sus_guid not in locations:
            continue
        # Time left at base decay rate (no police).  Police nearby slow decay,
        # so the actual expiry may be later — the 10-second refresh loop will
        # renew the flag before then.
        duration_seconds = math.ceil(
            wanted.wanted_remaining / BASE_DECAY_PER_TICK * TICK_INTERVAL
        )
        try:
            await make_suspect(
                http_client_mod, sus_guid, duration_seconds=max(1, duration_seconds)
            )
        except Exception:
            logger.warning("Failed to make suspect for %s", wanted.character.name)


async def tick_criminal_record_decay(http_client_mod=None) -> None:
    """Decay confiscatable_amount for ONLINE characters only.

    Called every minute via arq cron. Applies exponential decay with a
    2-hour half-life of *online time*. Offline criminals preserve their
    confiscatable amount so they cannot escape punishment by logging off.

    Players currently in a modded vehicle are excluded from decay —
    their confiscatable amount is preserved as long as they remain in
    a modified vehicle.

    The `amount` field is NEVER decayed — it is a permanent audit trail.
    """
    online_cutoff = timezone.now() - timedelta(seconds=ONLINE_THRESHOLD_SECONDS)
    records = [
        r
        async for r in CriminalRecord.objects.filter(
            cleared_at__isnull=True,
            confiscatable_amount__gt=0,
            character__last_online__gte=online_cutoff,
        ).select_related("character")
    ]
    if not records:
        return

    modded_guids: set[str] = set()
    if http_client_mod:
        from amc.mod_server import get_player_last_vehicle, get_player_last_vehicle_parts
        from amc.mod_detection import detect_custom_parts

        for record in records:
            guid = record.character.guid
            if not guid:
                continue
            try:
                last_vehicle, parts_data = await asyncio.gather(
                    get_player_last_vehicle(http_client_mod, guid),
                    get_player_last_vehicle_parts(http_client_mod, guid, complete=False),
                )
                main_vehicle = last_vehicle.get("vehicle")
                if not main_vehicle:
                    modded_guids.add(guid)
                    continue
                whitelist = None
                is_on_duty = await PoliceSession.objects.filter(
                    character=record.character, ended_at__isnull=True
                ).aexists()
                if is_on_duty:
                    whitelist = POLICE_DUTY_WHITELIST
                custom_parts = detect_custom_parts(
                    parts_data.get("parts", []), whitelist=whitelist
                )
                if custom_parts:
                    modded_guids.add(guid)
            except Exception:
                logger.debug(
                    "tick_criminal_record_decay: mod check failed for %s, skipping",
                    record.character.name,
                )

    decayed = []
    for record in records:
        if record.character.guid in modded_guids:
            continue
        record.confiscatable_amount = int(
            record.confiscatable_amount * CRIMINAL_RECORD_DECAY_FACTOR
        )
        if record.confiscatable_amount < CRIMINAL_RECORD_DECAY_FLOOR:
            record.confiscatable_amount = 0
        decayed.append(record)

    if not decayed:
        return
    await CriminalRecord.objects.abulk_update(decayed, ["confiscatable_amount"])
    logger.debug(
        "tick_criminal_record_decay: decayed %d record(s) (skipped %d modded)",
        len(decayed),
        len(modded_guids),
    )
