import math

from amc.command_framework import registry, CommandContext
from amc.game_server import get_players
from amc.models import Character, Wanted
from amc.mod_server import get_player, make_suspect, send_system_message, teleport_player
from amc.police import (
    activate_police,
    deactivate_police,
    get_active_police_characters,
    is_police,
    calculate_police_level,
)
from amc.special_cargo import create_or_refresh_wanted
from amc.utils import fuzzy_find_player
from datetime import timedelta
from django.utils import timezone
from django.utils.translation import gettext as _, gettext_lazy

from amc.commands.faction import parse_location_string

SETWANTED_COOLDOWN = timedelta(hours=1)
SETWANTED_MIN_DISTANCE = 300_000  # 3km = 300,000 units (1m = 100 units)

POLICE_STATIONS = [
    ("Jeju Police Station", -42361, -141792, -21094),
    ("Hallim Police Station", -325934, -2506, -21920),
    ("Seoguipo Police Station", -8776, 144044, -21084),
    ("Seongsan Police Station", 319727, -84041, -21921),
    ("Gapa Police Station", 77156, 648911, -9011),
    ("Gwangjin Police Station", 266983, 878250, -8911),
    ("Ara Police Station", 315281, 1335754, -19911),
]


def _distance_3d(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


@registry.register(
    "/police",
    description=gettext_lazy("Toggle police duty on/off"),
    category="Faction",
    featured=True,
)
async def cmd_police(ctx: CommandContext):
    active = await is_police(ctx.character)

    if active:
        await deactivate_police(ctx.character, ctx.http_client_mod)
        await send_system_message(
            ctx.http_client_mod,
            _("You are now off duty."),
            character_guid=ctx.character.guid,
        )
        await ctx.announce(f"{ctx.character.name} is now off police duty.")
    else:
        # Wanted criminals may not become police
        has_wanted = await Wanted.objects.filter(
            character=ctx.character, expired_at__isnull=True
        ).aexists()
        if has_wanted:
            await send_system_message(
                ctx.http_client_mod,
                _("You cannot go on police duty while you are wanted."),
                character_guid=ctx.character.guid,
            )
            return

        # Fetch live player list for location and vehicle check
        players = await get_players(ctx.http_client)
        pdata = None
        for uid, p in players:
            if str(uid) == str(ctx.player.unique_id):
                pdata = p
                break

        # Vehicle check
        if pdata and bool(pdata.get("vehicle")):
            await ctx.reply(
                _("<Title>Cannot Go On Duty</>\n\nPlease exit the vehicle first.")
            )
            return

        # Parse location and teleport to nearest police station
        if pdata and pdata.get("location"):
            try:
                loc = parse_location_string(pdata["location"])
                nearest = None
                min_dist = float("inf")
                for name, tx, ty, tz in POLICE_STATIONS:
                    dist = _distance_3d(loc, (tx, ty, tz))
                    if dist < min_dist:
                        min_dist = dist
                        nearest = (name, tx, ty, tz)
                if nearest:
                    station_name, tx, ty, tz = nearest
                    await teleport_player(
                        ctx.http_client_mod,
                        str(ctx.player.unique_id),
                        {"X": tx, "Y": ty, "Z": tz},
                        no_vehicles=True,
                    )
            except ValueError:
                pass

        await activate_police(ctx.character, ctx.http_client_mod)
        level = calculate_police_level(ctx.character.police_confiscated_total)
        await send_system_message(
            ctx.http_client_mod,
            _("You are now on police duty (Level {level}).").format(level=level),
            character_guid=ctx.character.guid,
        )
        await ctx.announce(f"{ctx.character.name} is now on police duty (P{level})!")


@registry.register(
    "/setwanted",
    description=gettext_lazy("Set a player as wanted (police only)"),
    category="Faction",
)
async def cmd_setwanted(ctx: CommandContext, target_player_name: str):
    from amc.models import CriminalRecord

    # Only on-duty police can use this command
    if not await is_police(ctx.character):
        await ctx.reply(_("You must be on police duty to use this command."))
        return

    # Find the target player online
    players = await get_players(ctx.http_client)
    target_pid = fuzzy_find_player(players, target_player_name)

    if not target_pid:
        await ctx.reply(
            _(
                "<Title>Player not found</>\n\n"
                "Please make sure you typed the name correctly."
            )
        )
        return

    # Cannot set wanted on yourself
    if str(target_pid) == str(ctx.player.unique_id):
        await ctx.reply(_("You cannot set yourself as wanted."))
        return

    # Resolve the target character from the game data
    target_player_data = next(
        (p for pid, p in players if str(pid) == str(target_pid)), None
    )
    if not target_player_data:
        return

    try:
        target_character = await Character.objects.aget(
            guid=target_player_data["character_guid"]
        )
    except Character.DoesNotExist:
        await ctx.reply(_("Character not found in database."))
        return

    # Guard: target must not already be wanted
    already_wanted = await Wanted.objects.filter(
        character=target_character, expired_at__isnull=True
    ).aexists()
    if already_wanted:
        await ctx.reply(
            _("<Title>Already Wanted</>\n\n{name} is already wanted.").format(
                name=target_character.name
            )
        )
        return

    # Cooldown: 1 hour since last Wanted expiry
    last_expired = (
        await Wanted.objects.filter(
            character=target_character, expired_at__isnull=False
        )
        .order_by("-expired_at")
        .afirst()
    )
    if last_expired:
        cooldown_end = last_expired.expired_at + SETWANTED_COOLDOWN
        now = timezone.now()
        if now < cooldown_end:
            remaining = cooldown_end - now
            remaining_mins = int(remaining.total_seconds() / 60) + 1
            await ctx.reply(
                _(
                    "<Title>Cooldown Active</>\n\n"
                    "{name} was recently released. "
                    "You can set them as wanted again in {mins} minute(s)."
                ).format(name=target_character.name, mins=remaining_mins)
            )
            return

    # Distance check: target must be at least 3km away from any police officer
    target_location_str = target_player_data.get("location")
    if not target_location_str:
        await ctx.reply(
            _(
                "<Title>Location Unknown</>\n\n"
                "Cannot determine {name}'s location."
            ).format(name=target_character.name)
        )
        return

    try:
        target_loc = parse_location_string(target_location_str)
    except ValueError:
        await ctx.reply(
            _(
                "<Title>Location Unknown</>\n\n"
                "Cannot determine {name}'s location."
            ).format(name=target_character.name)
        )
        return

    police_chars = await get_active_police_characters()
    async for police_char in police_chars:
        police_player_data = next(
            (
                p
                for pid, p in players
                if p.get("character_guid") == str(police_char.guid)
            ),
            None,
        )
        if not police_player_data:
            continue
        police_location_str = police_player_data.get("location")
        if not police_location_str:
            continue
        try:
            police_loc = parse_location_string(police_location_str)
        except ValueError:
            continue

        dist = _distance_3d(target_loc, police_loc)
        if dist < SETWANTED_MIN_DISTANCE:
            await ctx.reply(
                _(
                    "<Title>Too Close</>\n\n"
                    "{name} is too close to a police officer. "
                    "You can only set wanted on players at least 3km away from any officer."
                ).format(name=target_character.name)
            )
            return

    # AFK check: police may not set AFK players as wanted
    target_live = await get_player(
        ctx.http_client, str(target_pid), force_refresh=True
    )
    if target_live and target_live.get("bAFK"):
        await ctx.reply(
            _(
                "<Title>Player AFK</>\n\n"
                "{name} is currently AFK and cannot be set as wanted."
            ).format(name=target_character.name)
        )
        return

    # Innocence check: CriminalRecord is the single source of truth.
    # A NULL cleared_at means the character has an active criminal record.
    has_criminal_record = await CriminalRecord.objects.filter(
        character=target_character, cleared_at__isnull=True
    ).aexists()

    if has_criminal_record:
        # Legitimate wanted — standard minimum bounty applied inside create_or_refresh_wanted
        bounty_amount = 0
        warning_note = ""
    else:
        # Innocent civilian — no financial penalty, just a warning note
        bounty_amount = 0
        warning_note = " WARNING: No recent illicit activity detected."

    # Create or refresh the wanted record
    await create_or_refresh_wanted(
        target_character, ctx.http_client_mod, amount=bounty_amount
    )

    # Flag the target as a suspect in-game
    await make_suspect(ctx.http_client_mod, target_character.guid)

    await ctx.reply(
        _("<Title>Wanted Set</>\n\n{name} is now wanted!{note}").format(
            name=target_character.name, note=warning_note
        )
    )
    await ctx.announce(
        f"{target_character.name} has been marked as wanted by {ctx.character.name}!"
    )
