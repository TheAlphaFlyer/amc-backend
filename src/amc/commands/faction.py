import asyncio
import math
import re
from django.core.cache import cache
from amc.command_framework import registry, CommandContext
from amc.game_server import get_players
from amc.models import Character, FactionChoice, FactionMembership, TeleportPoint
from amc.mod_server import force_exit_vehicle, teleport_player
from django.utils.translation import gettext as gettext, gettext_lazy

# 100 game units = 1 metre
ARREST_RADIUS = 500  # 5m — cop must be within 5m of criminal
STATIONARY_THRESHOLD = 100  # 1m — max movement per poll tick
ARREST_POLL_COUNT = 5  # 5 polls × 1s = 5 seconds
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
    description=gettext_lazy("Arrest nearby criminals (Cops only)"),
    category="Faction",
    featured=True,
)
async def cmd_arrest(ctx: CommandContext):
    # 1. Verify cop faction
    is_cop = await FactionMembership.objects.filter(
        player=ctx.player, faction=FactionChoice.COP
    ).aexists()
    if not is_cop:
        await ctx.reply(gettext("You must be a member of the Police faction to use this command."))
        return

    # 2. Cooldown check
    cooldown_key = f"arrest_cooldown:{ctx.player.unique_id}"
    if cache.get(cooldown_key):
        await ctx.reply(gettext("You must wait before making another arrest."))
        return

    # 3. Initial poll — get all player positions
    players = await get_players(ctx.http_client)
    if not players:
        await ctx.reply(gettext("Could not fetch player data."))
        return

    locations = _build_player_locations(players)

    # Find cop's own position
    cop_guid = ctx.character.guid
    if cop_guid not in locations:
        await ctx.reply(gettext("Could not determine your position."))
        return

    cop_uid, cop_loc, cop_has_vehicle = locations[cop_guid]

    # 4. Find nearby criminals
    other_guids = [g for g in locations if g != cop_guid]
    if not other_guids:
        await ctx.reply(gettext("No players nearby."))
        return

    # Batch query: which of these characters are in the criminal faction?
    criminal_guids = set()
    async for char in (
        Character.objects.filter(guid__in=other_guids)
        .select_related("player__faction_membership")
    ):
        try:
            if char.player.faction_membership.faction == FactionChoice.CRIMINAL:
                criminal_guids.add(char.guid)
        except FactionMembership.DoesNotExist:
            continue

    if not criminal_guids:
        await ctx.reply(gettext("No criminals nearby."))
        return

    # Filter to criminals within ARREST_RADIUS
    targets = {}  # guid → (unique_id, initial_loc, has_vehicle)
    for guid in criminal_guids:
        if guid not in locations:
            continue
        dist = _distance_3d(cop_loc, locations[guid][1])
        if dist <= ARREST_RADIUS:
            targets[guid] = locations[guid]

    if not targets:
        await ctx.reply(gettext("No criminals within arrest range."))
        return

    # Look up names for all targets
    target_chars = {}
    async for char in Character.objects.filter(guid__in=targets.keys()):
        target_chars[char.guid] = char

    target_names = [target_chars[g].name for g in targets if g in target_chars]
    names_str = ", ".join(target_names)

    # 5. Notify cop
    await ctx.reply(
        gettext("Arresting {names}… stand still for 5 seconds.").format(names=names_str)
    )

    # 6. Poll loop — check every second for 5 seconds
    for i in range(ARREST_POLL_COUNT):
        await asyncio.sleep(1)

        players = await get_players(ctx.http_client)
        if not players:
            await ctx.reply(gettext("Lost connection to server. Arrest cancelled."))
            return

        current_locations = _build_player_locations(players)

        # Check cop still online and stationary
        if cop_guid not in current_locations:
            return  # cop disconnected, silently abort

        cop_uid_now, current_cop_loc, cop_veh = current_locations[cop_guid]
        if _distance_3d(cop_loc, current_cop_loc) > STATIONARY_THRESHOLD:
            await ctx.reply(gettext("You moved. Arrest cancelled."))
            return

        # Check each target criminal
        for guid in list(targets.keys()):
            if guid not in current_locations:
                # Criminal went offline — remove from targets
                name = target_chars[guid].name if guid in target_chars else "Unknown"
                await ctx.reply(
                    gettext("{name} went offline. Removed from arrest.").format(name=name)
                )
                del targets[guid]
                continue

            crim_uid, current_criminal_loc, crim_veh = current_locations[guid]
            initial_loc = locations[guid][1]

            if _distance_3d(initial_loc, current_criminal_loc) > STATIONARY_THRESHOLD:
                name = target_chars[guid].name if guid in target_chars else "Unknown"
                await ctx.reply(
                    gettext("{name} moved. Removed from arrest.").format(name=name)
                )
                del targets[guid]
                continue

            if _distance_3d(current_cop_loc, current_criminal_loc) > ARREST_RADIUS:
                name = target_chars[guid].name if guid in target_chars else "Unknown"
                await ctx.reply(
                    gettext("{name} is no longer within range. Removed from arrest.").format(
                        name=name
                    )
                )
                del targets[guid]
                continue

            # Update vehicle status for arrest execution
            targets[guid] = (crim_uid, current_criminal_loc, crim_veh)

        if not targets:
            await ctx.reply(gettext("All targets escaped. Arrest cancelled."))
            return

    # 7. Execute arrests — teleport to jail
    try:
        jail_tp = await TeleportPoint.objects.aget(name__iexact="jail")
        jail_location = {"X": jail_tp.location.x, "Y": jail_tp.location.y, "Z": jail_tp.location.z}
    except TeleportPoint.DoesNotExist:
        await ctx.reply(gettext("Jail teleport point not configured. Contact an admin."))
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

        name = target_chars[guid].name if guid in target_chars else "Unknown"
        arrested_names.append(name)

    # Set cooldown
    cache.set(cooldown_key, True, timeout=ARREST_COOLDOWN)

    # Announce and confirm
    names_arrested = ", ".join(arrested_names)
    await ctx.announce(
        f"{names_arrested} arrested by {ctx.character.name}!"
    )
    await ctx.reply(
        gettext("{names} arrested and sent to jail.").format(names=names_arrested)
    )
