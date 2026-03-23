"""Dashboard Auth — JWT-based authentication for Discord Activity + Steam Login.

Provides:
- DashboardJWTBearer: Django Ninja auth class for validating JWTs → Player
- create_session_token: Issue a signed JWT after successful login
- discord_token_exchange: Exchange Discord OAuth2 code → access_token → Player
- verify_steam_openid: Validate Steam OpenID 2.0 callback → Player
"""

import logging
import time
from dataclasses import dataclass

import aiohttp
import jwt
from django.conf import settings
from ninja.security import HttpBearer

from amc.models import Player

log = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"

# Simple in-memory cache for Discord user info (keyed by access_token).
# Short TTL (~5 min) to avoid redundant Discord API calls during auth window.
_discord_user_cache: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 300  # seconds


@dataclass
class DashboardUser:
    """Authenticated dashboard user, attached to request.auth."""

    player: Player | None
    provider: str  # "discord" or "steam"
    discord_user: dict | None = None  # Discord user object (id, username, avatar)
    steam_id: int | None = None


class DashboardJWTBearer(HttpBearer):
    """Django Ninja auth class that validates dashboard JWTs."""

    async def authenticate(self, request, token: str) -> DashboardUser | None:
        try:
            payload = jwt.decode(
                token, settings.SECRET_KEY, algorithms=["HS256"]
            )
        except jwt.ExpiredSignatureError:
            return None
        except jwt.InvalidTokenError:
            return None

        provider = payload.get("provider", "unknown")
        player_id = payload.get("player_id")
        player = None

        if player_id is not None:
            try:
                player = await Player.objects.aget(unique_id=player_id)
            except Player.DoesNotExist:
                pass

        return DashboardUser(
            player=player,
            provider=provider,
            discord_user=payload.get("discord_user"),
            steam_id=payload.get("steam_id"),
        )


def create_session_token(
    player: Player | None,
    provider: str,
    *,
    discord_user: dict | None = None,
    steam_id: int | None = None,
) -> str:
    """Create a signed JWT session token."""
    payload = {
        "provider": provider,
        "player_id": player.unique_id if player else None,
        "exp": int(time.time()) + settings.DASHBOARD_JWT_EXPIRY_HOURS * 3600,
        "iat": int(time.time()),
    }
    if discord_user:
        # Store minimal Discord info in JWT for /auth/me
        payload["discord_user"] = {
            "id": discord_user["id"],
            "username": discord_user["username"],
            "global_name": discord_user.get("global_name"),
            "avatar": discord_user.get("avatar"),
        }
    if steam_id:
        payload["steam_id"] = steam_id
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


async def _fetch_discord_user(
    session: aiohttp.ClientSession, access_token: str
) -> dict | None:
    """Fetch Discord user from /users/@me, with in-memory cache."""
    # Check cache
    now = time.time()
    cached = _discord_user_cache.get(access_token)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    async with session.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
    ) as resp:
        if resp.status != 200:
            return None
        user = await resp.json()

    # Store in cache
    _discord_user_cache[access_token] = (user, now)

    # Evict expired entries (simple cleanup)
    expired = [k for k, (_, ts) in _discord_user_cache.items() if now - ts > _CACHE_TTL]
    for k in expired:
        _discord_user_cache.pop(k, None)

    return user


async def discord_token_exchange(code: str) -> tuple[dict | None, Player | None]:
    """Exchange Discord OAuth2 code for access_token, fetch user, resolve Player.

    Returns (discord_user, player). discord_user is None if exchange fails.
    player is None if the Discord user is not verified in-game.
    """
    client_id = settings.DASHBOARD_DISCORD_CLIENT_ID
    client_secret = settings.DASHBOARD_DISCORD_CLIENT_SECRET

    if not client_id or not client_secret:
        log.error("DASHBOARD_DISCORD_CLIENT_ID/SECRET not configured")
        return None, None

    redirect_uri = f"https://{client_id}.discordsays.com"

    async with aiohttp.ClientSession() as session:
        # Step 1: Exchange code for access_token
        async with session.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                log.warning("Discord token exchange failed: %s", data)
                return None, None
            access_token = data["access_token"]

        # Step 2: Fetch Discord user
        discord_user = await _fetch_discord_user(session, access_token)
        if not discord_user:
            return None, None

    # Step 3: Resolve to Player
    discord_id = int(discord_user["id"])
    try:
        player = await Player.objects.aget(discord_user_id=discord_id)
    except Player.DoesNotExist:
        player = None

    return discord_user, player


# ---------------------------------------------------------------------------
# Steam OpenID 2.0
# ---------------------------------------------------------------------------

STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"


def build_steam_openid_redirect_url(callback_url: str) -> str:
    """Build the Steam OpenID login redirect URL."""
    from urllib.parse import urlencode

    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": callback_url,
        "openid.realm": callback_url.rsplit("/", 1)[0] + "/",
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    return f"{STEAM_OPENID_URL}?{urlencode(params)}"


async def verify_steam_openid(params: dict) -> tuple[int | None, Player | None]:
    """Validate Steam OpenID callback params and resolve to Player.

    Returns (steam_id, player). steam_id is None if validation fails.
    player is None if no Player exists with that Steam ID.
    """
    import re

    # Verify the claimed_id format
    claimed_id = params.get("openid.claimed_id", "")
    match = re.match(
        r"^https://steamcommunity\.com/openid/id/(\d+)$", claimed_id
    )
    if not match:
        log.warning("Invalid Steam claimed_id: %s", claimed_id)
        return None, None

    steam_id = int(match.group(1))

    # Validate with Steam by replaying the assertion
    validation_params = dict(params)
    validation_params["openid.mode"] = "check_authentication"

    async with aiohttp.ClientSession() as session:
        async with session.post(
            STEAM_OPENID_URL,
            data=validation_params,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            body = await resp.text()
            if "is_valid:true" not in body:
                log.warning("Steam OpenID validation failed: %s", body)
                return None, None

    # Resolve to Player
    try:
        player = await Player.objects.aget(unique_id=steam_id)
    except Player.DoesNotExist:
        player = None

    return steam_id, player
