from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

POSITION_UPDATE_RATE = 1
POSITION_UPDATE_SLEEP = 1.0 / POSITION_UPDATE_RATE
HEARTBEAT_INTERVAL = 15
MOD_PLAYERS_CACHE_TTL = 2
POLICE_ONLINE_THRESHOLD_SECONDS = 60


async def _get_hidden_player_unique_ids():
    from amc.models import PoliceSession, Wanted

    wanted_ids: set[int] = set()
    async for uid in Wanted.objects.filter(
        wanted_remaining__gt=0, expired_at__isnull=True
    ).values_list("character__player__unique_id", flat=True):
        wanted_ids.add(uid)

    online_threshold = timezone.now() - timedelta(seconds=POLICE_ONLINE_THRESHOLD_SECONDS)
    police_ids: set[int] = set()
    async for uid in PoliceSession.objects.filter(
        ended_at__isnull=True, character__last_online__gte=online_threshold
    ).values_list("character__player__unique_id", flat=True):
        police_ids.add(uid)

    return wanted_ids, police_ids


def _should_hide_player(player: dict, wanted_ids: set[int], police_ids: set[int], any_wanted: bool) -> bool:
    try:
        uid = int(player.get("UniqueID", 0))
    except (ValueError, TypeError):
        return False
    if uid in wanted_ids:
        return True
    if any_wanted and uid in police_ids:
        return True
    return False


async def get_players_mod(
    session,
    cache_key: str = "mod_players_list_all",
    cache_ttl: int = MOD_PLAYERS_CACHE_TTL,
    filter_hidden: bool = False,
):
    cached_data = cache.get(cache_key)
    if cached_data is not None:
        if not filter_hidden:
            return cached_data
        wanted_ids, police_ids = await _get_hidden_player_unique_ids()
        any_wanted = bool(wanted_ids)
        if not any_wanted and not police_ids:
            return cached_data
        return [
            p for p in cached_data
            if not _should_hide_player(p, wanted_ids, police_ids, any_wanted)
        ]

    async with session.get("/players") as resp:
        data = await resp.json()
        if not data or not data.get("data"):
            return []
        players = data["data"]
    cache.set(cache_key, players, timeout=cache_ttl)

    if filter_hidden:
        wanted_ids, police_ids = await _get_hidden_player_unique_ids()
        any_wanted = bool(wanted_ids)
        if wanted_ids or police_ids:
            return [
                p for p in players
                if not _should_hide_player(p, wanted_ids, police_ids, any_wanted)
            ]

    return players
