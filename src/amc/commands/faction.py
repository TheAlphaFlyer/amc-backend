import asyncio
import math
import re
from django.core.cache import cache
from amc.command_framework import registry, CommandContext
from amc.game_server import get_players
from amc.models import ArrestZone, Character, FactionChoice, FactionMembership, TeleportPoint
from amc.mod_server import force_exit_vehicle, send_system_message, show_popup, teleport_player
from django.utils.translation import gettext as gettext, gettext_lazy

# 100 game units = 1 metre
ARREST_RADIUS = 1500  # 15m — cop must be within 15m of suspect
SUSPECT_SPEED_LIMIT = 500  # 5m per poll tick — suspects moving faster are removed
ARREST_POLL_COUNT = 3  # 3 polls × 1s = 3 seconds
ARREST_COOLDOWN = 60  # seconds between arrests per cop

_LOC_RE = re.compile(
    r"X=(?P<x>[-\d.]+)\s+Y=(?P<y>[-\d.]+)\s+Z=(?P<z>[-\d.]+)"
)


def parse_location_string(loc_str: str) -> tuple[float, float, float]:
    """Parse 'X=-53918.590 Y=153629.920 Z=-20901.710' → (x, y, z)."""
    m = _LOC_RE.search(loc_str)
    if not m:
        raise ValueError(f"Cannot parse location: {loc_str}")
    return float(m["x"]), float(m["y"]), float(m["z"])


def _distance_3d(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _build_player_locations(players: list) -> dict[str, tuple[str, tuple[float, float, float], bool]]:
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
        has_vehicle = "vehicle" in pdata
        result[guid] = (pdata["unique_id"], loc, has_vehicle)
    return result


@registry.register(
    ["/arrest", "/a"],
    description=gettext_lazy("Arrest nearby suspects (Cops only)"),
    category="Faction",
    featured=True,
)
async def cmd_arrest(ctx: CommandContext):
    # 1. Verify cop faction
    is_cop = await FactionMembership.objects.filter(
        player=ctx.player, faction=FactionChoice.COP
    ).aexists()
    if not is_cop:
        await send_system_message(ctx.http_client_mod, gettext("You must be a member of the Police faction to use this command."), character_guid=ctx.character.guid)
        return

    # 2. Cooldown check
    cooldown_key = f"arrest_cooldown:{ctx.player.unique_id}"
    if cache.get(cooldown_key):
        await send_system_message(ctx.http_client_mod, gettext("You must wait before making another arrest."), character_guid=ctx.character.guid)
        return

    # 3. Initial poll — get all player positions
    players = await get_players(ctx.http_client)
    if not players:
        await send_system_message(ctx.http_client_mod, gettext("Could not fetch player data."), character_guid=ctx.character.guid)
        return

    locations = _build_player_locations(players)

    # Find cop's own position
    cop_guid = ctx.character.guid
    if cop_guid not in locations:
        await send_system_message(ctx.http_client_mod, gettext("Could not determine your position."), character_guid=ctx.character.guid)
        return

    cop_uid, cop_loc, cop_has_vehicle = locations[cop_guid]

    # 3b. Zone check — cop must be inside an active ArrestZone
    from django.contrib.gis.geos import Point
    cop_point = Point(cop_loc[0], cop_loc[1], srid=3857)
    zones_exist = await ArrestZone.objects.filter(active=True).aexists()
    if zones_exist:
        in_zone = await ArrestZone.objects.filter(
            active=True, polygon__contains=cop_point
        ).aexists()
        if not in_zone:
            await send_system_message(ctx.http_client_mod, gettext("Arrests can only be made in designated arrest zones."), character_guid=ctx.character.guid)
            return

    # 4. Find nearby non-police players
    other_guids = [g for g in locations if g != cop_guid]
    if not other_guids:
        await send_system_message(ctx.http_client_mod, gettext("No players nearby."), character_guid=ctx.character.guid)
        return

    # Batch query: exclude characters who are in the police faction
    cop_guids = set()
    async for char in (
        Character.objects.filter(guid__in=other_guids)
        .select_related("player__faction_membership")
    ):
        try:
            if char.player.faction_membership.faction == FactionChoice.COP:
                cop_guids.add(char.guid)
        except FactionMembership.DoesNotExist:
            continue

    suspect_guids = [g for g in other_guids if g not in cop_guids]

    if not suspect_guids:
        await send_system_message(ctx.http_client_mod, gettext("No suspects nearby."), character_guid=ctx.character.guid)
        return

    # Filter to suspects within ARREST_RADIUS
    targets = {}  # guid → (unique_id, initial_loc, has_vehicle)
    for guid in suspect_guids:
        if guid not in locations:
            continue
        dist = _distance_3d(cop_loc, locations[guid][1])
        if dist <= ARREST_RADIUS:
            targets[guid] = locations[guid]

    if not targets:
        await send_system_message(ctx.http_client_mod, gettext("No suspects within arrest range."), character_guid=ctx.character.guid)
        return

    # Look up names for all targets
    target_chars = {}
    async for char in Character.objects.filter(guid__in=targets.keys()):
        target_chars[char.guid] = char

    target_names = [target_chars[g].name for g in targets if g in target_chars]
    names_str = ", ".join(target_names)

    # 5. Notify cop
    await send_system_message(
        ctx.http_client_mod,
        gettext("Arresting {names}… stay close for 3 seconds.").format(names=names_str),
        character_guid=ctx.character.guid,
    )

    # Track previous suspect positions for speed check
    prev_suspect_locs = {guid: targets[guid][1] for guid in targets}

    # 6. Poll loop — check every second for 3 seconds
    for i in range(ARREST_POLL_COUNT):
        await asyncio.sleep(1)

        players = await get_players(ctx.http_client)
        if not players:
            await send_system_message(ctx.http_client_mod, gettext("Lost connection to server. Arrest cancelled."), character_guid=ctx.character.guid)
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
                    gettext("{name} went offline. Removed from arrest.").format(name=name),
                    character_guid=ctx.character.guid,
                )
                del targets[guid]
                prev_suspect_locs.pop(guid, None)
                continue

            crim_uid, current_criminal_loc, crim_veh = current_locations[guid]

            # Speed check: suspect must not be moving too fast
            prev_loc = prev_suspect_locs[guid]
            suspect_speed = _distance_3d(prev_loc, current_criminal_loc)
            if suspect_speed > SUSPECT_SPEED_LIMIT:
                name = target_chars[guid].name if guid in target_chars else "Unknown"
                await send_system_message(
                    ctx.http_client_mod,
                    gettext("{name} is moving too fast. Removed from arrest.").format(name=name),
                    character_guid=ctx.character.guid,
                )
                del targets[guid]
                prev_suspect_locs.pop(guid, None)
                continue

            # Proximity check: cop must stay within radius of suspect
            if _distance_3d(current_cop_loc, current_criminal_loc) > ARREST_RADIUS:
                name = target_chars[guid].name if guid in target_chars else "Unknown"
                await send_system_message(
                    ctx.http_client_mod,
                    gettext("{name} is no longer within range. Removed from arrest.").format(
                        name=name
                    ),
                    character_guid=ctx.character.guid,
                )
                del targets[guid]
                prev_suspect_locs.pop(guid, None)
                continue

            # Update for next tick
            prev_suspect_locs[guid] = current_criminal_loc
            targets[guid] = (crim_uid, current_criminal_loc, crim_veh)

        if not targets:
            await send_system_message(ctx.http_client_mod, gettext("All targets escaped. Arrest cancelled."), character_guid=ctx.character.guid)
            return

    # 7. Execute arrests — teleport to jail
    try:
        jail_tp = await TeleportPoint.objects.aget(name__iexact="jail")
        jail_location = {"X": jail_tp.location.x, "Y": jail_tp.location.y, "Z": jail_tp.location.z}
    except TeleportPoint.DoesNotExist:
        await send_system_message(ctx.http_client_mod, gettext("Jail teleport point not configured. Contact an admin."), character_guid=ctx.character.guid)
        return

    arrested_names = []
    for guid, (crim_uid, crim_loc, has_vehicle) in targets.items():
        # Exit vehicle
        if has_vehicle:
            try:
                await force_exit_vehicle(ctx.http_client_mod, guid)
                await asyncio.sleep(0.3)
            except Exception:
                pass

        # Teleport to jail
        await teleport_player(
            ctx.http_client_mod,
            crim_uid,
            jail_location,
            no_vehicles=True,
        )

        # Popup notification
        await show_popup(
            ctx.http_client_mod,
            "You have been arrested!",
            player_id=crim_uid,
        )

        name = target_chars[guid].name if guid in target_chars else "Unknown"
        arrested_names.append(name)

    # Set cooldown
    cache.set(cooldown_key, True, timeout=ARREST_COOLDOWN)

    # Announce and confirm
    names_arrested = ", ".join(arrested_names)
    await ctx.announce(
        f"{names_arrested} arrested by {ctx.character.name}!"
    )
    await send_system_message(
        ctx.http_client_mod,
        gettext("{names} arrested and sent to jail.").format(names=names_arrested),
        character_guid=ctx.character.guid,
    )
