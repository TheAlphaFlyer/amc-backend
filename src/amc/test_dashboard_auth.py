"""Tests for Dashboard Auth — Discord Activity + Steam Login."""

import time
from typing import cast, Any
from unittest.mock import patch

import jwt
from django.conf import settings
from django.test import TestCase
from ninja.testing import TestAsyncClient

from amc.api.auth_routes import auth_router
from amc.factories import PlayerFactory
from amc_backend.dashboard_auth import create_session_token
from asgiref.sync import sync_to_async


class DiscordTokenExchangeTest(TestCase):
    """Test POST /auth/discord/token"""

    def setUp(self):
        self.api_client = TestAsyncClient(auth_router)

    @patch("amc.api.auth_routes.discord_token_exchange")
    async def test_discord_exchange_verified_player(self, mock_exchange):
        """Discord user with verified Player gets session_token + player info."""
        player = await sync_to_async(PlayerFactory)(
            unique_id=1001, discord_user_id=123456789
        )
        discord_user = {
            "id": "123456789",
            "username": "testuser",
            "global_name": "Test User",
            "avatar": "abc123",
        }
        mock_exchange.return_value = (discord_user, player)

        response = await cast(
            Any,
            self.api_client.post(
                "/discord/token",
                json={"code": "test-auth-code"},
            ),
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("session_token", data)
        self.assertIsNotNone(data["player"])
        self.assertEqual(data["player"]["unique_id"], player.unique_id)
        self.assertEqual(data["discord_user"]["username"], "testuser")
        mock_exchange.assert_awaited_once_with("test-auth-code")

    @patch("amc.api.auth_routes.discord_token_exchange")
    async def test_discord_exchange_unverified_user(self, mock_exchange):
        """Discord user without matching Player gets token but no player info."""
        discord_user = {
            "id": "999999999",
            "username": "unverified_user",
            "global_name": None,
            "avatar": None,
        }
        mock_exchange.return_value = (discord_user, None)

        response = await cast(
            Any,
            self.api_client.post(
                "/discord/token",
                json={"code": "test-auth-code"},
            ),
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("session_token", data)
        self.assertIsNone(data.get("player"))

    @patch("amc.api.auth_routes.discord_token_exchange")
    async def test_discord_exchange_failed(self, mock_exchange):
        """Failed Discord token exchange returns 400."""
        mock_exchange.return_value = (None, None)

        response = await cast(
            Any,
            self.api_client.post(
                "/discord/token",
                json={"code": "bad-code"},
            ),
        )

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertEqual(data["error"], "token_exchange_failed")


class SteamCallbackTest(TestCase):
    """Test GET /auth/steam/callback"""

    def setUp(self):
        self.api_client = TestAsyncClient(auth_router)

    @patch("amc.api.auth_routes.verify_steam_openid")
    async def test_steam_callback_valid_player(self, mock_verify):
        """Valid Steam OpenID with matching Player returns session_token."""
        player = await sync_to_async(PlayerFactory)(unique_id=76561198012345678)
        mock_verify.return_value = (76561198012345678, player)

        response = await cast(
            Any,
            self.api_client.get(
                "/steam/callback?"
                "openid.claimed_id=https://steamcommunity.com/openid/id/76561198012345678"
                "&openid.mode=id_res"
                "&openid.signed=signed,op_endpoint"
            ),
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("session_token", data)
        self.assertIsNotNone(data["player"])
        self.assertEqual(data["player"]["unique_id"], player.unique_id)

    @patch("amc.api.auth_routes.verify_steam_openid")
    async def test_steam_callback_unknown_player(self, mock_verify):
        """Valid Steam OpenID with no matching Player returns token but no player."""
        mock_verify.return_value = (99999999999999999, None)

        response = await cast(
            Any,
            self.api_client.get(
                "/steam/callback?"
                "openid.claimed_id=https://steamcommunity.com/openid/id/99999999999999999"
                "&openid.mode=id_res"
            ),
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("session_token", data)
        self.assertIsNone(data.get("player"))

    @patch("amc.api.auth_routes.verify_steam_openid")
    async def test_steam_callback_invalid(self, mock_verify):
        """Failed Steam validation returns 400."""
        mock_verify.return_value = (None, None)

        response = await cast(
            Any,
            self.api_client.get(
                "/steam/callback?"
                "openid.claimed_id=https://evil.com/id/12345"
                "&openid.mode=id_res"
            ),
        )

        self.assertEqual(response.status_code, 400)

    @patch("amc.api.auth_routes.verify_steam_openid")
    async def test_steam_callback_validation_failed(self, mock_verify):
        """Steam returning is_valid:false results in 400."""
        mock_verify.return_value = (None, None)

        response = await cast(
            Any,
            self.api_client.get(
                "/steam/callback?"
                "openid.claimed_id=https://steamcommunity.com/openid/id/76561198012345678"
                "&openid.mode=id_res"
            ),
        )

        self.assertEqual(response.status_code, 400)


class JWTValidationTest(TestCase):
    """Test JWT token creation and validation via /auth/me."""

    def setUp(self):
        self.api_client = TestAsyncClient(auth_router)

    async def test_valid_jwt_with_player(self):
        """Valid JWT with player returns player info."""
        player = await sync_to_async(PlayerFactory)(
            unique_id=2001, discord_user_id=111222333
        )
        token = create_session_token(
            player,
            "discord",
            discord_user={
                "id": "111222333",
                "username": "tester",
                "global_name": "Tester",
                "avatar": None,
            },
        )

        response = await cast(
            Any,
            self.api_client.get(
                "/me",
                headers={"Authorization": f"Bearer {token}"},
            ),
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["provider"], "discord")
        self.assertIsNotNone(data["player"])
        self.assertEqual(data["player"]["unique_id"], player.unique_id)
        self.assertEqual(data["discord_user"]["username"], "tester")

    async def test_valid_jwt_without_player(self):
        """Valid JWT with no player (unverified user) returns null player."""
        token = create_session_token(
            None,
            "discord",
            discord_user={
                "id": "999",
                "username": "noone",
                "global_name": None,
                "avatar": None,
            },
        )

        response = await cast(
            Any,
            self.api_client.get(
                "/me",
                headers={"Authorization": f"Bearer {token}"},
            ),
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data["player"])

    async def test_expired_jwt(self):
        """Expired JWT returns 401."""
        payload = {
            "provider": "discord",
            "player_id": None,
            "exp": int(time.time()) - 3600,  # expired 1 hour ago
            "iat": int(time.time()) - 7200,
        }
        token = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")

        response = await cast(
            Any,
            self.api_client.get(
                "/me",
                headers={"Authorization": f"Bearer {token}"},
            ),
        )

        self.assertEqual(response.status_code, 401)

    async def test_malformed_jwt(self):
        """Malformed JWT returns 401."""
        response = await cast(
            Any,
            self.api_client.get(
                "/me",
                headers={"Authorization": "Bearer not-a-real-jwt"},
            ),
        )

        self.assertEqual(response.status_code, 401)

    async def test_no_auth_header(self):
        """No auth header returns 401."""
        response = await cast(Any, self.api_client.get("/me"))
        self.assertEqual(response.status_code, 401)


class SteamLoginRedirectTest(TestCase):
    """Test GET /auth/steam/login"""

    def setUp(self):
        self.api_client = TestAsyncClient(auth_router)

    async def test_steam_login_returns_redirect_url(self):
        """Steam login endpoint returns a valid OpenID redirect URL."""
        callback = "https://dashboard.aseanmotorclub.com/auth/steam/callback"
        response = await cast(
            Any, self.api_client.get(f"/steam/login?callback_url={callback}")
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("redirect_url", data)
        self.assertIn("steamcommunity.com/openid/login", data["redirect_url"])
        self.assertIn("checkid_setup", data["redirect_url"])


class PrivacyTest(TestCase):
    """Ensure sensitive fields are never exposed through auth endpoints."""

    def setUp(self):
        self.api_client = TestAsyncClient(auth_router)

    async def test_me_does_not_expose_sensitive_fields(self):
        """GET /auth/me never exposes money, social_score, or raw discord_user_id."""
        player = await sync_to_async(PlayerFactory)(
            unique_id=3001, discord_user_id=444555666, social_score=42
        )
        token = create_session_token(player, "discord")

        response = await cast(
            Any,
            self.api_client.get(
                "/me",
                headers={"Authorization": f"Bearer {token}"},
            ),
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Player info should only contain safe fields
        player_data = data["player"]
        self.assertNotIn("discord_user_id", player_data)
        self.assertNotIn("money", player_data)
        self.assertNotIn("social_score", player_data)
