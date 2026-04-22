from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase

from amc.game_server import get_admins, get_player_info, _NEG_SENTINEL


class GetAdminsTests(SimpleTestCase):
    """Unit tests for get_admins — no DB needed."""

    async def test_returns_cached_when_available(self):
        """When the admin list is already cached, return it immediately."""
        with patch("amc.game_server.cache.get", return_value={"123", "456"}):
            result = await get_admins(AsyncMock())
        self.assertEqual(result, {"123", "456"})

    async def test_parses_admin_dict(self):
        """Parses the nested admin dict from the game server."""
        mock_session = AsyncMock()
        mock_response = {
            "succeeded": True,
            "data": {
                "admin": {
                    "0": {"unique_id": "A1B2", "nickname": "Alice"},
                    "1": {"unique_id": "C3D4", "nickname": "Bob"},
                }
            },
        }

        with patch("amc.game_server.game_api_request", AsyncMock(return_value=mock_response)):
            with patch("amc.game_server.cache.get", return_value=None):
                with patch("amc.game_server.cache.set") as mock_cache_set:
                    result = await get_admins(mock_session)

        self.assertEqual(result, {"A1B2", "C3D4"})
        mock_cache_set.assert_called_once()
        _, _, kwargs = mock_cache_set.mock_calls[0]
        self.assertEqual(kwargs["timeout"], 60)

    async def test_returns_empty_set_on_api_failure(self):
        """When the API raises an exception, return an empty set."""
        with patch("amc.game_server.game_api_request", AsyncMock(side_effect=Exception("timeout"))):
            with patch("amc.game_server.cache.get", return_value=None):
                result = await get_admins(AsyncMock())

        self.assertEqual(result, set())

    async def test_returns_empty_set_on_missing_data(self):
        """When the response has no admin data, return an empty set."""
        mock_response = {"succeeded": True, "data": {}}

        with patch("amc.game_server.game_api_request", AsyncMock(return_value=mock_response)):
            with patch("amc.game_server.cache.get", return_value=None):
                with patch("amc.game_server.cache.set") as mock_cache_set:
                    result = await get_admins(AsyncMock())

        self.assertEqual(result, set())
        mock_cache_set.assert_called_once()

    async def test_skips_entries_without_unique_id(self):
        """Entries missing unique_id are skipped."""
        mock_response = {
            "succeeded": True,
            "data": {
                "admin": {
                    "0": {"unique_id": "VALID", "nickname": "Alice"},
                    "1": {"nickname": "Bob"},  # missing unique_id
                    "2": None,  # null entry
                }
            },
        }

        with patch("amc.game_server.game_api_request", AsyncMock(return_value=mock_response)):
            with patch("amc.game_server.cache.get", return_value=None):
                result = await get_admins(AsyncMock())

        self.assertEqual(result, {"VALID"})


class GetPlayerInfoTests(SimpleTestCase):
    """Unit tests for get_player_info — no DB needed."""

    def _make_player_list_response(self, players):
        """Build a /player/list response with the given players."""
        return {
            "succeeded": True,
            "data": {str(i): p for i, p in enumerate(players) if p is not None},
        }

    async def test_returns_cached_when_available(self):
        """When player info is already cached, return it immediately."""
        cached = {"CharacterGuid": "GUID", "PlayerName": "Test"}
        with patch("amc.game_server.cache.get", return_value=cached):
            result = await get_player_info(AsyncMock(), "12345")
        self.assertEqual(result, cached)

    async def test_returns_none_for_cached_negative(self):
        """When the negative sentinel is cached, return None."""
        with patch("amc.game_server.cache.get", return_value=_NEG_SENTINEL):
            result = await get_player_info(AsyncMock(), "12345")
        self.assertIsNone(result)

    async def test_returns_none_when_player_not_found(self):
        """When the player is not in the list, cache negative and return None."""
        response = self._make_player_list_response([
            {"unique_id": "99999", "character_guid": "OTHER", "name": "Other"},
        ])

        with patch("amc.game_server.game_api_request", AsyncMock(return_value=response)):
            with patch("amc.game_server.cache.set") as mock_cache_set:
                result = await get_player_info(AsyncMock(), "12345")

        self.assertIsNone(result)
        mock_cache_set.assert_called_once()
        _, args, _ = mock_cache_set.mock_calls[0]
        self.assertEqual(args[1], _NEG_SENTINEL)

    async def test_returns_normalized_player_info(self):
        """Returns a normalized dict matching mod_server.get_player shape."""
        response = self._make_player_list_response([
            {
                "unique_id": "12345",
                "character_guid": "abcd1234",
                "name": "TestPlayer",
                "location": "X=100.0 Y=200.0 Z=300.0",
                "vehicle": {"name": "Scooty", "unique_id": 175114},
            },
        ])

        with patch("amc.game_server.game_api_request", AsyncMock(return_value=response)):
            with patch("amc.game_server.get_admins", AsyncMock(return_value={"12345"})):
                with patch("amc.game_server.cache.set"):
                    result = await get_player_info(AsyncMock(), "12345")

        self.assertIsNotNone(result)
        self.assertEqual(result["CharacterGuid"], "ABCD1234")
        self.assertEqual(result["PlayerName"], "TestPlayer")
        self.assertEqual(result["Location"], {"X": 100.0, "Y": 200.0, "Z": 300.0})
        self.assertEqual(result["VehicleKey"], "Scooty")
        self.assertTrue(result["bIsAdmin"])
        self.assertEqual(result["unique_id"], "12345")

    async def test_returns_none_location_when_empty(self):
        """When location string is empty, Location is None."""
        response = self._make_player_list_response([
            {
                "unique_id": "12345",
                "character_guid": "abcd1234",
                "name": "TestPlayer",
                "location": "",
            },
        ])

        with patch("amc.game_server.game_api_request", AsyncMock(return_value=response)):
            with patch("amc.game_server.get_admins", AsyncMock(return_value=set())):
                with patch("amc.game_server.cache.set"):
                    result = await get_player_info(AsyncMock(), "12345")

        self.assertIsNotNone(result)
        self.assertIsNone(result["Location"])
        self.assertEqual(result["VehicleKey"], "None")
        self.assertFalse(result["bIsAdmin"])

    async def test_force_refresh_bypasses_cache(self):
        """force_refresh=True ignores the cache."""
        cached = {"CharacterGuid": "OLD", "PlayerName": "Old"}
        response = self._make_player_list_response([
            {
                "unique_id": "12345",
                "character_guid": "NEW1234",
                "name": "NewPlayer",
            },
        ])

        with patch("amc.game_server.cache.get", return_value=cached):
            with patch("amc.game_server.game_api_request", AsyncMock(return_value=response)):
                with patch("amc.game_server.get_admins", AsyncMock(return_value=set())):
                    with patch("amc.game_server.cache.set"):
                        result = await get_player_info(AsyncMock(), "12345", force_refresh=True)

        self.assertEqual(result["CharacterGuid"], "NEW1234")

    async def test_matches_by_string_comparison(self):
        """player_id comparison is string-safe (int vs str)."""
        response = self._make_player_list_response([
            {
                "unique_id": "12345",
                "character_guid": "ABCD",
                "name": "Test",
            },
        ])

        with patch("amc.game_server.game_api_request", AsyncMock(return_value=response)):
            with patch("amc.game_server.get_admins", AsyncMock(return_value=set())):
                with patch("amc.game_server.cache.set"):
                    result = await get_player_info(AsyncMock(), 12345)

        self.assertIsNotNone(result)
        self.assertEqual(result["CharacterGuid"], "ABCD")
