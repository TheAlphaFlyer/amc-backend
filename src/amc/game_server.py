import asyncio
from typing import Any, cast
import urllib.parse
import aiohttp
from yarl import URL
from django.core.cache import cache


async def game_api_request(
    session, url, method="get", password="", params={}, timeout=15
):
    req_params = {"password": password, **params}
    params_str = urllib.parse.urlencode(
        req_params, quote_via=cast(Any, urllib.parse.quote)
    )
    try:
        fn = getattr(session, method)
    except AttributeError as e:
        print(f"Invalid method: {e}")
        raise e

    request_timeout = aiohttp.ClientTimeout(total=timeout)
    async with fn(
        URL(f"{url}?{params_str}", encoded=True), timeout=request_timeout
    ) as resp:
        resp_json = await resp.json()
        return resp_json


async def get_players(session, password=""):
    cache_key = "game_online_players_list"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    data = await game_api_request(session, "/player/list")
    if "data" not in data:
        return []
    players = [
        (player["unique_id"], player)
        for player in data["data"].values()
        if player is not None
    ]
    cache.set(cache_key, players, timeout=1)
    return players


def _parse_location_string(loc_str):
    """Parse native game API location string (e.g. 'X=123.4 Y=567.8 Z=-910.1') into a dict."""
    result = {}
    for part in loc_str.split():
        axis, _, value = part.partition("=")
        result[axis.lower()] = float(value)
    return result


async def get_players_with_location(session):
    """Return player location data from the native game API.

    Returns a list of dicts with the same keys that monitor_locations expects:
      CharacterGuid, Location {x, y, z}, VehicleKey, UniqueID, PlayerName

    Uses the native /player/list endpoint (no game-thread pressure) with a 1s
    Redis cache, replacing the Lua mod server GET /players for location polling.
    """
    cache_key = "game_players_with_location"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    data = await game_api_request(session, "/player/list")
    if not data.get("succeeded") or "data" not in data:
        return []

    from amc.enums import VehicleKeyByLabel

    result = []
    for player in data["data"].values():
        if player is None:
            continue
        loc_str = player.get("location", "")
        if not loc_str:
            continue
        try:
            location = _parse_location_string(loc_str)
        except (ValueError, AttributeError):
            continue

        vehicle_info = player.get("vehicle")
        vehicle_name = vehicle_info["name"] if vehicle_info else None
        # Map display name → DB key; fall back to raw name for unlisted vehicles
        vehicle_key = VehicleKeyByLabel.get(vehicle_name, vehicle_name) if vehicle_name else "None"

        result.append({
            "CharacterGuid": player["character_guid"].upper(),
            "UniqueID": player["unique_id"],
            "PlayerName": player["name"],
            "Location": location,
            "VehicleKey": vehicle_key,
        })

    cache.set(cache_key, result, timeout=1)
    return result


async def is_player_online(player_id, session, password=""):
    players = await get_players(session, password)
    player_ids = {str(player_id) for player_id, _ in players}
    return str(player_id) in player_ids


async def announcement_request(
    message, session, password="", type="message", color=None
):
    params = {"message": message}
    if type:
        params["type"] = type
    if color is not None:
        params["color"] = color
    return await game_api_request(session, "/chat", method="post", params=params)


async def announce(
    message: str,
    session,
    password="",
    clear_banner=True,
    type="message",
    color="FFFF00",
    delay=0,
):
    if delay > 0:
        await asyncio.sleep(delay)
    message_sanitized = message.strip().replace("\n", " ")
    try:
        await announcement_request(
            message_sanitized, session, password, type=type, color=color
        )
        if type == "announce" and clear_banner:
            await announcement_request(" ", session, password)
    except Exception as e:
        print(f"Error sending message: {e}")
        raise e


async def get_deliverypoints(session, password=""):
    return await game_api_request(session, "/delivery/sites")


async def get_world(session, password=""):
    return await game_api_request(
        session, "https://server.aseanmotorclub.com/api/world/"
    )


async def kick_player(unique_id, session):
    params = {
        "unique_id": unique_id,
    }
    return await game_api_request(session, "/player/kick", method="post", params=params)


async def ban_player(session, unique_id, hours=None, reason=None):
    params = {
        "unique_id": unique_id,
    }
    if hours:
        params["hours"] = hours
    if reason:
        params["reason"] = reason
    return await game_api_request(session, "/player/ban", method="post", params=params)
