from amc.command_framework import registry, CommandContext
from amc.mod_server import send_system_message
from amc.police import (
    activate_police,
    deactivate_police,
    is_police,
    calculate_police_level,
)
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
        await activate_police(ctx.character, ctx.http_client_mod)
        level = calculate_police_level(ctx.character.police_confiscated_total)
        await send_system_message(
            ctx.http_client_mod,
            _("You are now on police duty (Level {level}).").format(level=level),
            character_guid=ctx.character.guid,
        )
        await ctx.announce(f"{ctx.character.name} is now on police duty (P{level})!")
