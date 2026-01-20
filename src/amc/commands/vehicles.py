from amc.command_framework import registry, CommandContext
from django.utils.translation import gettext_lazy

@registry.register(
    ["/despawn", "/d"], 
    description=gettext_lazy("Despawn your vehicle"), 
    category="Vehicle Management",
    #deprecated=True,
    #deprecated_message="<Title>Command Deprecated</Title>\nThe /despawn command is no longer available.\nVehicles now despawn automatically."
)
async def cmd_despawn(ctx: CommandContext, category: str = "all"):
    # Deprecated - handled by the command framework
    pass

