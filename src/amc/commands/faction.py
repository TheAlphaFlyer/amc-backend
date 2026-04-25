import asyncio
import logging
import math
import re

from django.core.cache import cache
from django.utils import timezone
from django.utils.translation import gettext as gettext, gettext_lazy

from amc.command_framework import registry, CommandContext
from amc.game_server import announce, get_players
from amc.models import (
    ArrestZone,
    Character,
    PoliceSession,
    TeleportPoint,
    Confiscation,
    Wanted,
)
from amc.mod_server import (
    force_exit_vehicle,
    get_player,
    send_system_message,
    show_popup,
    teleport_player,
    transfer_money,
)
from amc_finance.services import (
    record_treasury_confiscation_income,
)
from amc.pipeline.profit import on_player_profit
from amc.police import (
    get_active_police_characters,
    is_police_vehicle,
    record_confiscation_for_level,
)
from datetime import timedelta
from amc.player_tags import refresh_player_name

logger = logging.getLogger("amc.commands.faction")

# 100 game units = 1 metre
ARREST_RADIUS_ON_FOOT = 3000  # 30m — cop on foot (consistent with auto-arrest)
ARREST_RADIUS_IN_VEHICLE = 2000  # 20m — cop in vehicle (consistent with auto-arrest)
SUSPECT_SPEED_LIMIT = 556  # ~5.56m/s ≈ 20km/h — suspects moving faster are immune
ARREST_POLL_COUNT = 3  # 3 polls × 1s = 3 seconds (consistent with auto-arrest)
ARREST_POLL_INTERVAL = 1  # seconds between polls
ARREST_COOLDOWN = 0  # seconds between arrests per cop

_LOC_RE = re.compile(r"X=(?P<x>[-\d.]+)\s+Y=(?P<y>[-\d.]+)\s+Z=(?P<z>[-\d.]+)")


def parse_location_string(loc_str: str) -> tuple[float, float, float]:
    """Parse 'X=-53918.590 Y=153629.920 Z=-20901.710' → (x, y, z)."""
    m = _LOC_RE.search(loc_str)
    if not m:
        raise ValueError(f"Cannot parse location: {loc_str}")
    return float(m["x"]), float(m["y"]), float(m["z"])


def _distance_3d(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _build_player_locations(
    players: list,
) -> dict[str, tuple[str, tuple[float, float, float], bool]]:
    """Build guid → (unique_id, (x,y,z), has_vehicle) mapping from game server player list."""
    result = {}
    for _uid, pdata in players:
        guid = pdata.get("character_guid")
        loc_str = pdata.get("location")
        if not guid or not loc_str:
            continue
        try:
            loc = parse_location_string(loc_str)
        except ValueError:
            continue
        has_vehicle = bool(pdata.get("vehicle"))
        result[guid] = (pdata["unique_id"], loc, has_vehicle)
    return result


async def execute_arrest(
    officer_character,
    targets: dict,
    target_chars: dict,
    http_client,
    http_client_mod,
) -> tuple[list[str], int]:
    """Execute arrest: teleport to jail, confiscate money, announce.

    Args:
        officer_character: The arresting officer's Character model, or None for
            a system/automated arrest.  When None, no reward is paid to an
            officer and no officer-specific messages are sent.
        targets: guid -> (unique_id, location, has_vehicle) for each suspect.
        target_chars: guid -> Character model for each suspect.
        http_client: Game server HTTP client (for announcements).
        http_client_mod: Mod server HTTP client (for teleport, money, messages).

    Returns:
        (arrested_names, total_confiscated) tuple.
    """
    try:
        jail_tp = await TeleportPoint.objects.aget(name__iexact="jail")
        jail_location = {
            "X": jail_tp.location.x,
            "Y": jail_tp.location.y,
            "Z": jail_tp.location.z,
        }
    except TeleportPoint.DoesNotExist:
        raise ValueError("Jail teleport point not configured.")

    arrested_names = []
    total_confiscated = 0
    for guid, (crim_uid, crim_loc, has_vehicle) in targets.items():
        name = target_chars[guid].name if guid in target_chars else "Unknown"

        # --- Phase 1: Expire Wanted & confiscate amount (BEFORE teleport) ---
        # We must expire the Wanted record before teleporting to jail.
        # Otherwise the ServerTeleportCharacter event handler will see an
        # active Wanted and apply a second penalty.
        suspect_char = target_chars.get(guid)
        confiscated_amount = 0
        if suspect_char:
            wanted = None
            try:
                wanted = await Wanted.objects.aget(
                    character=suspect_char, expired_at__isnull=True
                )
            except Wanted.DoesNotExist:
                pass

            bounty = 0
            if wanted:
                # Bounty component of confiscation (may be negative for wrongful wanted)
                bounty = wanted.amount

                # Expire Wanted status BEFORE teleport
                wanted.wanted_remaining = 0
                wanted.expired_at = timezone.now()
                await wanted.asave(update_fields=["wanted_remaining", "expired_at"])

                # Strip the wanted star tag from the player's display name
                asyncio.create_task(
                    refresh_player_name(suspect_char, http_client_mod)
                )

            # Delivery confiscation: use confiscatable_amount from active CriminalRecord
            from amc.models import CriminalRecord

            active_record = await CriminalRecord.objects.filter(
                character=suspect_char, cleared_at__isnull=True
            ).afirst()
            delivery_confiscation = active_record.confiscatable_amount if active_record else 0
            confiscated_amount = bounty + delivery_confiscation

            # Create a Confiscation record for the arrest
            confiscation = await Confiscation.objects.acreate(
                character=suspect_char,
                officer=officer_character,  # None for system arrests
                cargo_key="Illicit",
                amount=confiscated_amount,
            )

            # Clear the CriminalRecord and refresh tag (removes [C] indicator)
            if active_record:
                active_record.cleared_at = timezone.now()
                active_record.cleared_by_arrest = confiscation
                await active_record.asave(update_fields=["cleared_at", "cleared_by_arrest"])
                # Refresh tag to remove [C] (Wanted refresh above only fires if wanted existed)
                if not wanted:
                    asyncio.create_task(
                        refresh_player_name(suspect_char, http_client_mod)
                    )

            if confiscated_amount > 0:
                # --- Legitimate arrest: confiscate delivery earnings from laundered total ---
                await suspect_char.arefresh_from_db(fields=["criminal_laundered_total"])
                new_criminal_total = max(
                    0, suspect_char.criminal_laundered_total - delivery_confiscation
                )
                suspect_char.criminal_laundered_total = new_criminal_total
                await suspect_char.asave(update_fields=["criminal_laundered_total"])

                await transfer_money(
                    http_client_mod,
                    int(-confiscated_amount),
                    "Money Confiscated",
                    str(suspect_char.player_id),
                )

                await record_treasury_confiscation_income(
                    confiscated_amount, "Police Confiscation"
                )

                # --- Spread confiscation to all online police ---
                online_police = [
                    c
                    async for c in await get_active_police_characters()
                ]
                # Filter out AFK officers — they don't receive rewards
                active_police = []
                for officer in online_police:
                    player_data = await get_player(
                        http_client_mod, str(officer.player_id)
                    )
                    if player_data and player_data.get("bAFK"):
                        continue
                    active_police.append(officer)
                if active_police:
                    per_officer_money = max(
                        1, confiscated_amount // len(active_police)
                    )
                    for officer in active_police:
                        await record_confiscation_for_level(
                            officer,
                            confiscated_amount,
                            http_client=http_client,
                            session=http_client_mod,
                        )
                        await transfer_money(
                            http_client_mod,
                            int(per_officer_money),
                            "Confiscation Reward",
                            str(officer.player_id),
                        )
                        await on_player_profit(
                            officer,
                            0,
                            per_officer_money,
                            http_client_mod,
                            http_client,
                        )
                        await send_system_message(
                            http_client_mod,
                            gettext(
                                "Confiscated ${total:,} in illegal earnings from {name}. Your share: ${share:,}."
                            ).format(
                                total=confiscated_amount,
                                name=name,
                                share=per_officer_money,
                            ),
                            character_guid=officer.guid,
                        )

                await send_system_message(
                    http_client_mod,
                    gettext(
                        "Police confiscated ${amount:,} in illegal earnings from your account."
                    ).format(amount=confiscated_amount),
                    character_guid=suspect_char.guid,
                )

            # (no financial action for zero-amount arrests)

        total_confiscated += confiscated_amount


        # --- Phase 2: Physical arrest (teleport to jail) ---
        # Always attempt to exit vehicle — snapshot may be stale
        try:
            await force_exit_vehicle(http_client_mod, guid)
            await asyncio.sleep(1.5)
        except Exception:
            pass

        # Teleport to jail — try without vehicle first, fallback to with-vehicle
        try:
            await teleport_player(
                http_client_mod,
                crim_uid,
                jail_location,
                no_vehicles=True,
                force=True,
            )
        except Exception:
            # Player still in vehicle — teleport with vehicle as fallback
            try:
                await teleport_player(
                    http_client_mod,
                    crim_uid,
                    jail_location,
                    no_vehicles=False,
                    force=True,
                )
            except Exception:
                continue  # teleport failed but confiscation already recorded

        # Popup notification
        await show_popup(
            http_client_mod,
            "You have been arrested!",
            player_id=crim_uid,
        )

        # Mark character as jailed so monitor_locations enforces jail perimeter
        if suspect_char:
            suspect_char.jailed_until = timezone.now() + timedelta(seconds=60)
            await suspect_char.asave(update_fields=["jailed_until"])

        arrested_names.append(name)

    return arrested_names, total_confiscated


async def perform_arrest(
    officer_character,
    targets: dict,
    target_chars: dict,
    http_client,
    http_client_mod,
    officer_message_format: str = "{names} arrested and sent to jail.",
) -> tuple[list[str], int]:
    """Execute arrest and send standard officer notification + server announcement.

    Wraps :func:`execute_arrest` with uniform post-arrest messaging so callers
    do not duplicate notification logic.

    Raises:
        ValueError: If jail teleport point is not configured.
    """
    arrested_names, total_confiscated = await execute_arrest(
        officer_character=officer_character,
        targets=targets,
        target_chars=target_chars,
        http_client=http_client,
        http_client_mod=http_client_mod,
    )

    if arrested_names and officer_character:
        names_arrested = ", ".join(arrested_names)
        await send_system_message(
            http_client_mod,
            officer_message_format.format(names=names_arrested),
            character_guid=officer_character.guid,
        )
        if total_confiscated > 0:
            await announce(
                f"{names_arrested} arrested by {officer_character.name}! "
                f"${total_confiscated:,} confiscated.",
                http_client,
            )
        else:
            await announce(
                f"{names_arrested} arrested by {officer_character.name}!",
                http_client,
            )

    return arrested_names, total_confiscated


@registry.register(
    ["/arrest", "/a"],
    description=gettext_lazy("Arrest nearby suspects (Cops only)"),
    category="Faction",
    featured=True,
)
async def cmd_arrest(ctx: CommandContext):
    """Arrest nearby suspects.

    Unlike auto-arrest, manual /arrest can arrest any nearby suspect
    regardless of Wanted status.  Confiscation only applies if the
    suspect has an active Wanted record (handled inside execute_arrest).
    """
    # 1. Verify active police session
    is_cop = await PoliceSession.objects.filter(
        character=ctx.character, ended_at__isnull=True
    ).aexists()
    if not is_cop:
        await send_system_message(
            ctx.http_client_mod,
            gettext(
                "You must be on police duty to use this command. Use /police to start."
            ),
            character_guid=ctx.character.guid,
        )
        return

    # 2. Cooldown check
    cooldown_key = f"arrest_cooldown:{ctx.player.unique_id}"
    if cache.get(cooldown_key):
        await send_system_message(
            ctx.http_client_mod,
            gettext("You must wait before making another arrest."),
            character_guid=ctx.character.guid,
        )
        return

    # 3. Initial poll — get all player positions
    players = await get_players(ctx.http_client)
    if not players:
        await send_system_message(
            ctx.http_client_mod,
            gettext("Could not fetch player data."),
            character_guid=ctx.character.guid,
        )
        return

    locations = _build_player_locations(players)

    # Find cop's own position
    cop_guid = ctx.character.guid
    if cop_guid not in locations:
        await send_system_message(
            ctx.http_client_mod,
            gettext("Could not determine your position."),
            character_guid=ctx.character.guid,
        )
        return

    cop_uid, cop_loc, cop_has_vehicle = locations[cop_guid]

    # 3a. Police vehicle check — cop in non-police vehicle cannot arrest
    if cop_has_vehicle:
        vehicle_names: dict[str, str | None] = {}
        for _uid, pdata in players:
            guid = pdata.get("character_guid")
            if guid:
                vehicle = pdata.get("vehicle")
                if isinstance(vehicle, dict):
                    vehicle_names[guid] = vehicle.get("name")
                else:
                    vehicle_names[guid] = vehicle if vehicle else None
        if not is_police_vehicle(vehicle_names.get(cop_guid)):
            await send_system_message(
                ctx.http_client_mod,
                gettext(
                    "You must be on foot or in a police vehicle to make an arrest."
                ),
                character_guid=ctx.character.guid,
            )
            return

    # 3b. Zone check — cop must be inside an active ArrestZone
    from django.contrib.gis.geos import Point

    cop_point = Point(cop_loc[0], cop_loc[1], srid=3857)
    zones_exist = await ArrestZone.objects.filter(active=True).aexists()
    if zones_exist:
        in_zone = await ArrestZone.objects.filter(
            active=True, polygon__contains=cop_point
        ).aexists()
        if not in_zone:
            await send_system_message(
                ctx.http_client_mod,
                gettext("Arrests can only be made in designated arrest zones."),
                character_guid=ctx.character.guid,
            )
            return

    # 4. Find nearby non-police players
    other_guids = [g for g in locations if g != cop_guid]
    if not other_guids:
        await send_system_message(
            ctx.http_client_mod,
            gettext("No players nearby."),
            character_guid=ctx.character.guid,
        )
        return

    # Batch query: exclude characters who have active police sessions
    cop_guids = set()
    async for char in Character.objects.filter(guid__in=other_guids):
        has_session = await PoliceSession.objects.filter(
            character=char, ended_at__isnull=True
        ).aexists()
        if has_session:
            cop_guids.add(char.guid)

    suspect_guids = [g for g in other_guids if g not in cop_guids]

    if not suspect_guids:
        await send_system_message(
            ctx.http_client_mod,
            gettext("No suspects nearby."),
            character_guid=ctx.character.guid,
        )
        return

    # Filter to suspects within arrest radius (depends on whether cop is on foot or in vehicle)
    arrest_radius = (
        ARREST_RADIUS_IN_VEHICLE if cop_has_vehicle else ARREST_RADIUS_ON_FOOT
    )
    targets = {}  # guid → (unique_id, initial_loc, has_vehicle)
    for guid in suspect_guids:
        if guid not in locations:
            continue
        dist = _distance_3d(cop_loc, locations[guid][1])
        if dist <= arrest_radius:
            targets[guid] = locations[guid]

    if not targets:
        await send_system_message(
            ctx.http_client_mod,
            gettext("No suspects within arrest range."),
            character_guid=ctx.character.guid,
        )
        return

    # Look up Character models for all targets
    target_chars = {}
    async for char in Character.objects.filter(guid__in=targets.keys()).select_related(
        "player"
    ):
        target_chars[char.guid] = char

    if not targets:
        await send_system_message(
            ctx.http_client_mod,
            gettext("No suspects within arrest range."),
            character_guid=ctx.character.guid,
        )
        return

    target_names = [target_chars[g].name for g in targets if g in target_chars]
    names_str = ", ".join(target_names)

    # 5. Notify cop
    await send_system_message(
        ctx.http_client_mod,
        gettext("Arresting {names}… stay close for {seconds} seconds.").format(
            names=names_str, seconds=ARREST_POLL_COUNT
        ),
        character_guid=ctx.character.guid,
    )

    # Track previous suspect positions for speed check
    prev_suspect_locs = {guid: targets[guid][1] for guid in targets}

    # 6. Poll loop — check every second for 3 seconds
    for i in range(ARREST_POLL_COUNT):
        await asyncio.sleep(ARREST_POLL_INTERVAL)

        players = await get_players(ctx.http_client)
        if not players:
            await send_system_message(
                ctx.http_client_mod,
                gettext("Lost connection to server. Arrest cancelled."),
                character_guid=ctx.character.guid,
            )
            return

        current_locations = _build_player_locations(players)

        # Check cop still online
        if cop_guid not in current_locations:
            return  # cop disconnected, silently abort

        cop_uid_now, current_cop_loc, cop_veh = current_locations[cop_guid]

        # Check each target criminal
        for guid in list(targets.keys()):
            if guid not in current_locations:
                # Criminal went offline — remove from targets
                name = target_chars[guid].name if guid in target_chars else "Unknown"
                await send_system_message(
                    ctx.http_client_mod,
                    gettext("{name} went offline. Removed from arrest.").format(
                        name=name
                    ),
                    character_guid=ctx.character.guid,
                )
                del targets[guid]
                prev_suspect_locs.pop(guid, None)
                continue

            crim_uid, current_criminal_loc, crim_veh = current_locations[guid]

            # Speed check: only for suspects in vehicles (on-foot suspects
            # are always arrestable within radius regardless of speed)
            prev_loc = prev_suspect_locs[guid]
            distance_moved = _distance_3d(prev_loc, current_criminal_loc)
            speed_per_second = distance_moved / ARREST_POLL_INTERVAL
            if crim_veh and speed_per_second > SUSPECT_SPEED_LIMIT:
                name = target_chars[guid].name if guid in target_chars else "Unknown"
                await send_system_message(
                    ctx.http_client_mod,
                    gettext("{name} is moving too fast. Removed from arrest.").format(
                        name=name
                    ),
                    character_guid=ctx.character.guid,
                )
                del targets[guid]
                prev_suspect_locs.pop(guid, None)
                continue

            # Proximity check: cop must stay within radius of suspect
            current_radius = (
                ARREST_RADIUS_IN_VEHICLE if cop_veh else ARREST_RADIUS_ON_FOOT
            )
            if _distance_3d(current_cop_loc, current_criminal_loc) > current_radius:
                name = target_chars[guid].name if guid in target_chars else "Unknown"
                await send_system_message(
                    ctx.http_client_mod,
                    gettext(
                        "{name} is no longer within range. Removed from arrest."
                    ).format(name=name),
                    character_guid=ctx.character.guid,
                )
                del targets[guid]
                prev_suspect_locs.pop(guid, None)
                continue

            # Update for next tick
            prev_suspect_locs[guid] = current_criminal_loc
            targets[guid] = (crim_uid, current_criminal_loc, crim_veh)

        if not targets:
            await send_system_message(
                ctx.http_client_mod,
                gettext("All targets escaped. Arrest cancelled."),
                character_guid=ctx.character.guid,
            )
            return

    # 7. Execute arrests
    try:
        arrested_names, total_confiscated = await perform_arrest(
            officer_character=ctx.character,
            targets=targets,
            target_chars=target_chars,
            http_client=ctx.http_client,
            http_client_mod=ctx.http_client_mod,
            officer_message_format=gettext("{names} arrested and sent to jail."),
        )
    except ValueError as e:
        await send_system_message(
            ctx.http_client_mod, gettext(str(e)), character_guid=ctx.character.guid
        )
        return

    if not arrested_names:
        await send_system_message(
            ctx.http_client_mod,
            gettext("All targets escaped. Arrest cancelled."),
            character_guid=ctx.character.guid,
        )
        return

    # Set cooldown
    cache.set(cooldown_key, True, timeout=ARREST_COOLDOWN)
