import psutil  # type: ignore[import-untyped]
from amc.mod_server import get_status, set_config
from amc.game_server import get_players, announce
from amc.models import ServerStatus
from amc.utils import skip_if_running


@skip_if_running
async def monitor_server_status(ctx):
    status = await get_status(ctx["http_client_mod"])
    try:
        players = await get_players(ctx["http_client"])
    except Exception as e:
        print(f"Failed to get players: {e}")
        players = []

    if status is None:
        status = {}

    mem = psutil.virtual_memory()
    await ServerStatus.objects.acreate(
        fps=status.get("FPS", 0),
        used_memory=mem.used,
        num_players=len(players) if players is not None else 0,
    )


async def monitor_server_condition(ctx):
    status = await get_status(ctx["http_client_mod"])
    try:
        players = await get_players(ctx["http_client"])
    except Exception as e:
        print(f"Failed to get players: {e}")
        players = []

    if status is None:
        status = {}
    fps = status.get("FPS", 0)
    num_players = len(players) if players is not None else 0
    base_vehicles_per_player = 12
    target_fps = 22
    if num_players == 0:
        max_vehicles_per_player = base_vehicles_per_player
    else:
        max_vehicles_per_player = (
            min(
                base_vehicles_per_player,
                max(
                    int(fps * base_vehicles_per_player * 20 / target_fps / num_players),
                    3,
                ),
            )
            - 1
        )

    await set_config(ctx["http_client_mod"], max_vehicles_per_player)
    if fps < target_fps:
        if max_vehicles_per_player < base_vehicles_per_player:
            await announce(
                f"Max vehicles per player is now {max_vehicles_per_player}.",
                ctx["http_client"],
                color="FF59EE",
            )


async def monitor_rp_mode(ctx):
    # NOTE: Autopilot detection (bIsAIDriving) requires per-player vehicle data
    # that is no longer available via the batch GET /players endpoint. The
    # per-player list_player_vehicles endpoint is disabled. This function is
    # a no-op until a batch vehicle endpoint is added to the mod server.
    pass
