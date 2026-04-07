from amc.command_framework import registry, CommandContext
from amc.game_server import get_players
from amc.models import Character, Wanted
from amc.mod_server import send_system_message
from amc.police import (
    activate_police,
    deactivate_police,
    is_police,
    calculate_police_level,
)
from amc.special_cargo import create_or_refresh_wanted
from amc.utils import fuzzy_find_player
from django.utils.translation import gettext as _, gettext_lazy


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
    from datetime import timedelta

    from django.utils import timezone

    from amc.models import Delivery
    from amc.special_cargo import (
        ILLICIT_CARGO_KEYS,
        ILLICIT_DELIVERY_WINDOW,
    )

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

    # Innocence check: look for illicit deliveries in the past 10 minutes
    # that are NOT already linked to a prior Wanted record.
    # Deliveries linked to an existing Wanted have already been "accounted for"
    # and must not count again toward justifying a new wanted status.
    cutoff = timezone.now() - timedelta(seconds=ILLICIT_DELIVERY_WINDOW)
    has_recent_illicit = await Delivery.objects.filter(
        character=target_character,
        cargo_key__in=ILLICIT_CARGO_KEYS,
        timestamp__gte=cutoff,
        wanted__isnull=True,  # exclude deliveries already linked to a Wanted
    ).aexists()

    if has_recent_illicit:
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

    await ctx.reply(
        _("<Title>Wanted Set</>\n\n{name} is now wanted!{note}").format(
            name=target_character.name, note=warning_note
        )
    )
    await ctx.announce(
        f"{target_character.name} has been marked as wanted by {ctx.character.name}!"
    )
