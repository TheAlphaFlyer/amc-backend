from django.core.cache import cache

POSITION_UPDATE_RATE = 1
POSITION_UPDATE_SLEEP = 1.0 / POSITION_UPDATE_RATE
HEARTBEAT_INTERVAL = 15
MOD_PLAYERS_CACHE_TTL = 2


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
