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
from datetime import timedelta
from django.utils import timezone
from django.utils.translation import gettext as _, gettext_lazy

SETWANTED_COOLDOWN = timedelta(hours=1)


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

    await ctx.reply(
        _("<Title>Wanted Set</>\n\n{name} is now wanted!{note}").format(
            name=target_character.name, note=warning_note
        )
    )
    await ctx.announce(
        f"{target_character.name} has been marked as wanted by {ctx.character.name}!"
    )
