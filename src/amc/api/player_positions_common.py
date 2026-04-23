import re

from django.core.cache import cache

POSITION_UPDATE_RATE = 1
POSITION_UPDATE_SLEEP = 1.0 / POSITION_UPDATE_RATE
HEARTBEAT_INTERVAL = 15
MOD_PLAYERS_CACHE_TTL = 2

_STAR_RE = re.compile(r"\[\*+[^\]]*\] ")
_P_TAG_RE = re.compile(r"\[P\d+\] ")


def is_player_hidden(player: dict, has_star: bool) -> bool:
    name = player.get("PlayerName", "")
    return (
        _STAR_RE.search(name) is not None
        or (has_star and _P_TAG_RE.search(name) is not None)
    )


def build_player_positions(players: list[dict]) -> list[dict]:
    """Return a list of player-position dicts with hidden logic applied.

    Each dict contains: unique_id, player_name, x, y, z, hidden, vehicle_key.
    """
    has_star = any(_STAR_RE.search(p.get("PlayerName", "")) for p in players)
    result = []
    for p in players:
        loc = p.get("Location", {})
        hidden = is_player_hidden(p, has_star)
        if hidden:
            x = y = z = 0
        else:
            x = float(loc.get("X", 0))
            y = float(loc.get("Y", 0))
            z = float(loc.get("Z", 0))
        result.append(
            {
                "unique_id": p.get("UniqueID", 0),
                "player_name": p.get("PlayerName", ""),
                "x": x,
                "y": y,
                "z": z,
                "hidden": hidden,
                "vehicle_key": p.get("VehicleKey", ""),
            }
        )
    return result


async def get_players_mod(
    session,
    cache_key: str = "mod_players_list_all",
    cache_ttl: int = MOD_PLAYERS_CACHE_TTL,
):
    cached_data = cache.get(cache_key)
    if cached_data is not None:
        return cached_data

    async with session.get("/players") as resp:
        data = await resp.json()
        if not data or not data.get("data"):
            return []
        players = data["data"]
    cache.set(cache_key, players, timeout=cache_ttl)
    return players
