from typing import Optional
from amc.command_framework import registry, CommandContext
from amc.mod_server import list_player_vehicles
from amc.game_server import get_players
from amc.vehicles import format_vehicle_name
from amc.mod_detection import detect_custom_parts, format_custom_parts_game
from amc.utils import fuzzy_find_player
from django.utils.translation import gettext as _, gettext_lazy


@registry.register(
    ["/despawn", "/d"],
    description=gettext_lazy("Despawn your vehicle"),
    category="Vehicle Management",
    # deprecated=True,
    # deprecated_message="<Title>Command Deprecated</Title>\nThe /despawn command is no longer available.\nVehicles now despawn automatically."
)
async def cmd_despawn(ctx: CommandContext, category: str = "all"):
    # Deprecated - handled by the command framework
    pass


@registry.register(
    "/check_mods",
    description=gettext_lazy("Check a player's vehicle for custom parts"),
    category="Vehicle Management",
)
async def cmd_check_mods(ctx: CommandContext, target_player_name: Optional[str] = None):
    # Resolve target player ID (only admins can check other players)
    is_admin = ctx.player_info and ctx.player_info.get("bIsAdmin")
    if target_player_name and is_admin:
        players = await get_players(ctx.http_client)
        target_pid = fuzzy_find_player(players, target_player_name)
        if not target_pid:
            await ctx.reply(
                _("<Title>Player not found</>"
                  "\n\nCould not find a player matching that name.")
            )
            return
    else:
        target_pid = str(ctx.player.unique_id)
        target_player_name = ctx.character.name

    # Fetch only the active (main) vehicle
    try:
        player_vehicles = await list_player_vehicles(
            ctx.http_client_mod, target_pid, active=True, complete=True
        )
    except Exception:
        await ctx.reply(
            _("<Title>Error</>"
              "\n\nFailed to fetch vehicle data. Is the player online?")
        )
        return

    if not player_vehicles:
        await ctx.reply(
            _("<Title>No Vehicle</>"
              "\n\n{name} has no active vehicle.").format(
                name=target_player_name
            )
        )
        return

    # Check the first (main) vehicle
    v_id, vehicle = next(iter(player_vehicles.items()))
    vehicle_name = format_vehicle_name(vehicle["fullName"])
    parts = vehicle.get("parts", [])
    custom = detect_custom_parts(parts)

    if custom:
        await ctx.reply(
            _("<Title>Custom Parts Detected</>"
              "\n\n<Bold>{name}</> — {vehicle}"
              "\n{count} custom part(s):\n\n{parts}").format(
                name=target_player_name,
                vehicle=vehicle_name,
                count=len(custom),
                parts=format_custom_parts_game(custom),
            )
        )
    else:
        await ctx.reply(
            _("<Title>Parts Check</>"
              "\n\n<Bold>{name}</> — {vehicle}"
              "\n\nAll stock parts.").format(
                name=target_player_name,
                vehicle=vehicle_name,
            )
        )
