"""Dashboard auth API routes.

Endpoints for Discord Activity and Steam Login authentication flows.
After successful auth, returns a JWT session token for subsequent API calls.
"""

from ninja import Router, Schema

from amc_backend.dashboard_auth import (
    DashboardJWTBearer,
    DashboardUser,
    build_steam_openid_redirect_url,
    create_session_token,
    discord_token_exchange,
    verify_steam_openid,
)

auth_router = Router(tags=["auth"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DiscordTokenRequest(Schema):
    code: str


class PlayerInfo(Schema):
    unique_id: int
    discord_name: str | None = None
    verified: bool


class AuthResponse(Schema):
    session_token: str
    player: PlayerInfo | None = None
    discord_user: dict | None = None


class MeResponse(Schema):
    provider: str
    player: PlayerInfo | None = None
    discord_user: dict | None = None
    steam_id: int | None = None


class SteamLoginRedirect(Schema):
    redirect_url: str


class ErrorResponse(Schema):
    error: str
    detail: str | None = None


# ---------------------------------------------------------------------------
# Discord Activity auth
# ---------------------------------------------------------------------------


@auth_router.post(
    "/discord/token",
    response={200: AuthResponse, 400: ErrorResponse},
    summary="Exchange Discord OAuth2 code for session token",
)
async def discord_token(request, body: DiscordTokenRequest):
    """Exchange a Discord Activity OAuth2 code for a JWT session token.

    The frontend calls this after SDK.authorize() returns a code.
    """
    discord_user, player = await discord_token_exchange(body.code)
    if discord_user is None:
        return 400, {"error": "token_exchange_failed"}

    token = create_session_token(player, "discord", discord_user=discord_user)

    result = {"session_token": token, "discord_user": discord_user}
    if player:
        result["player"] = {
            "unique_id": player.unique_id,
            "discord_name": player.discord_name,
            "verified": player.verified,
        }
    return 200, result


# ---------------------------------------------------------------------------
# Steam OpenID auth
# ---------------------------------------------------------------------------


@auth_router.get(
    "/steam/login",
    response={200: SteamLoginRedirect},
    summary="Get Steam OpenID login redirect URL",
)
async def steam_login(request, callback_url: str):
    """Return the Steam OpenID redirect URL.

    The frontend redirects the user to this URL. After Steam login,
    the user is redirected back to callback_url with OpenID params.
    """
    redirect_url = build_steam_openid_redirect_url(callback_url)
    return {"redirect_url": redirect_url}


@auth_router.get(
    "/steam/callback",
    response={200: AuthResponse, 400: ErrorResponse},
    summary="Handle Steam OpenID callback",
)
async def steam_callback(request):
    """Validate Steam OpenID callback and return a JWT session token.

    The frontend calls this with the full query string from Steam's redirect.
    """
    params = dict(request.GET.items())
    steam_id, player = await verify_steam_openid(params)
    if steam_id is None:
        return 400, {"error": "steam_validation_failed"}

    token = create_session_token(player, "steam", steam_id=steam_id)

    result: dict = {"session_token": token}
    if player:
        result["player"] = {
            "unique_id": player.unique_id,
            "discord_name": player.discord_name,
            "verified": player.verified,
        }
    return 200, result


# ---------------------------------------------------------------------------
# Authenticated endpoints
# ---------------------------------------------------------------------------


@auth_router.get(
    "/me",
    response=MeResponse,
    auth=DashboardJWTBearer(),
    summary="Get current authenticated user info",
)
async def auth_me(request):
    """Return the current authenticated user's player info."""
    user: DashboardUser = request.auth
    result: dict = {
        "provider": user.provider,
        "discord_user": user.discord_user,
        "steam_id": user.steam_id,
    }
    if user.player:
        result["player"] = {
            "unique_id": user.player.unique_id,
            "discord_name": user.player.discord_name,
            "verified": user.player.verified,
        }
    return result
