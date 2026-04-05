"""Tests for the wanted countdown tick (amc.criminals)."""

from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone

from amc.criminals import _compute_stars, _last_star_notified, tick_wanted_countdown
from amc.factories import CharacterFactory, PlayerFactory
from amc.models import PoliceSession, Wanted


def _make_player_data(unique_id, character_guid, x, y, z):
    """Build a fake player dict matching the game server /player/list format."""
    return {
        "unique_id": str(unique_id),
        "character_guid": character_guid,
        "location": f"X={x} Y={y} Z={z}",
    }


def _make_players_list(player_datas):
    """Wrap player datas into the format returned by get_players()."""
    return [(d["unique_id"], d) for d in player_datas]


class ComputeStarsTests(TestCase):
    """Unit tests for _compute_stars helper.

    Formula: min(ceil(wanted_remaining / 60) + 1, 5)
    Star boundaries: 5 at 180-300s, 4 at 120-179s, 3 at 60-119s, 2 at 1-59s, 0 at 0s.
    """

    def test_300_seconds_is_5_stars(self):
        self.assertEqual(_compute_stars(300), 5)

    def test_240_seconds_is_5_stars(self):
        self.assertEqual(_compute_stars(240), 5)

    def test_181_seconds_is_5_stars(self):
        self.assertEqual(_compute_stars(181), 5)

    def test_180_seconds_is_4_stars(self):
        self.assertEqual(_compute_stars(180), 4)

    def test_179_seconds_is_4_stars(self):
        self.assertEqual(_compute_stars(179), 4)

    def test_120_seconds_is_3_stars(self):
        self.assertEqual(_compute_stars(120), 3)

    def test_60_seconds_is_2_stars(self):
        self.assertEqual(_compute_stars(60), 2)

    def test_1_second_is_2_stars(self):
        self.assertEqual(_compute_stars(1), 2)

    def test_0_seconds_is_0_stars(self):
        self.assertEqual(_compute_stars(0), 0)

    def test_negative_is_0_stars(self):
        self.assertEqual(_compute_stars(-10), 0)

    def test_all_boundaries(self):
        """Verify all star transition boundaries."""
        self.assertEqual(_compute_stars(300), 5)
        self.assertEqual(_compute_stars(181), 5)
        self.assertEqual(_compute_stars(180), 4)  # boundary
        self.assertEqual(_compute_stars(179), 4)
        self.assertEqual(_compute_stars(120), 3)  # boundary
        self.assertEqual(_compute_stars(119), 3)
        self.assertEqual(_compute_stars(60), 2)  # boundary
        self.assertEqual(_compute_stars(59), 2)
        self.assertEqual(_compute_stars(1), 2)
        self.assertEqual(_compute_stars(0), 0)


@patch("amc.criminals.refresh_player_name", new_callable=AsyncMock)
@patch("amc.criminals.send_system_message", new_callable=AsyncMock)
class WantedCountdownTickTests(TestCase):
    """Integration tests for tick_wanted_countdown."""

    def setUp(self):
        _last_star_notified.clear()

    async def _setup_criminal(self, wanted_remaining=300):
        """Create a criminal with wanted status."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        await Wanted.objects.acreate(
            character=character,
            wanted_remaining=wanted_remaining,
        )
        return character

    async def _setup_police(self):
        """Create an officer with active police session."""
        player = await sync_to_async(PlayerFactory)()
        officer = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await officer.asave(update_fields=["last_online"])
        await PoliceSession.objects.acreate(character=officer)
        return officer

    # --- No-cops immediate expiry tests ---

    async def test_no_cops_immediately_expires_online(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """With no cops online, online suspects get wanted cleared instantly."""
        criminal = await self._setup_criminal(wanted_remaining=300)

        players = _make_players_list(
            [
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 5000, 5000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)
        mock_sys_msg.assert_called_once_with(
            mock_http_mod,
            "Your wanted status has expired.",
            character_guid=criminal.guid,
        )
        mock_refresh.assert_called_once_with(criminal, mock_http_mod)

    async def test_no_cops_immediately_expires_offline(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """With no cops online, offline suspects get wanted cleared instantly."""
        criminal = await self._setup_criminal(wanted_remaining=300)

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=None
        ):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)
        mock_sys_msg.assert_not_called()  # offline — no message
        mock_refresh.assert_called_once_with(criminal, mock_http_mod)

    async def test_no_cops_clears_last_star_notified(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """_last_star_notified entry is removed when no-cops immediate expiry fires."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        _last_star_notified[criminal.guid] = 5

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=None
        ):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        self.assertNotIn(criminal.guid, _last_star_notified)

    # --- Countdown with cops present ---

    async def test_star_transitions_all_sent(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Stars drop from 5→0 as wanted_remaining decrements, sending 4 messages.

        Boundaries: 180(5→4), 120(4→3), 60(3→2), 0(2→0).
        Requires a cop present so the countdown ticks naturally.
        """
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 500000, 500000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 5000, 5000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            # 300 → 0 (300 ticks, crosses 180, 120, 60, 0)
            for _ in range(300):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        self.assertEqual(mock_sys_msg.call_count, 4)
        for call in mock_sys_msg.call_args_list:
            self.assertEqual(call.kwargs["character_guid"], criminal.guid)

    async def test_first_star_change_message(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """First star change (5→4 at 180s) sends '4 stars remaining'."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 500000, 500000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 5000, 5000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        # Tick 120 times: 300 → 180 (crosses 180 boundary)
        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(120):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_sys_msg.assert_called_once()
        self.assertIn("4 stars remaining", mock_sys_msg.call_args[0][1])

    async def test_refresh_player_name_on_expiry(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """When wanted_remaining hits 0, refresh_player_name is called."""
        criminal = await self._setup_criminal(wanted_remaining=1.5)
        officer = await self._setup_police()

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 500000, 500000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 5000, 5000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(3):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_refresh.assert_called_once_with(criminal, mock_http_mod)

    async def test_wanted_record_marked_expired(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Wanted record has expired_at set when remaining reaches 0."""
        criminal = await self._setup_criminal(wanted_remaining=1.5)
        officer = await self._setup_police()

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 500000, 500000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 5000, 5000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(3):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)

    async def test_offline_suspect_still_decrements(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Suspects not in the player list still have their wanted decremented when cops exist."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 500000, 500000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertAlmostEqual(wanted.wanted_remaining, 290, delta=1)

    async def test_no_messages_sent_to_offline_suspect(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """System messages are not sent to offline suspects even when stars change."""
        await self._setup_criminal(wanted_remaining=181)
        officer = await self._setup_police()

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 500000, 500000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            # Tick past the 180 boundary
            for _ in range(2):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_sys_msg.assert_not_called()

    async def test_nearby_cop_slows_decrement(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """With a nearby cop, wanted decrements slower than full speed."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 5000, 5000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 5010, 5000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertGreater(wanted.wanted_remaining, 295)

    async def test_distant_cop_full_speed(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """With a distant cop (>20km), countdown runs at full speed (not faster)."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 500000, 500000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 5000, 5000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertAlmostEqual(wanted.wanted_remaining, 290, delta=1)

    async def test_no_message_when_star_unchanged(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """No message is sent if star count hasn't changed."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 500000, 500000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 5000, 5000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_sys_msg.assert_not_called()

    async def test_no_wanted_records_skips_processing(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """If no wanted records exist, nothing happens."""
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_sys_msg.assert_not_called()
        mock_refresh.assert_not_called()

    async def test_last_star_notified_cleaned_up_on_expiry(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """_last_star_notified entry is removed when wanted expires via countdown."""
        criminal = await self._setup_criminal(wanted_remaining=1.5)
        officer = await self._setup_police()
        _last_star_notified[criminal.guid] = 5

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 500000, 500000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 5000, 5000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(3):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        self.assertNotIn(criminal.guid, _last_star_notified)

    async def test_star_message_at_180_boundary(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """At 181→180s, message says '4 stars remaining'."""
        criminal = await self._setup_criminal(wanted_remaining=181)
        officer = await self._setup_police()

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 500000, 500000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 5000, 5000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_sys_msg.assert_called_once()
        self.assertIn("4 stars remaining", mock_sys_msg.call_args[0][1])

    async def test_star_message_at_60_boundary(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """At 61→60s, message says '2 stars remaining'."""
        criminal = await self._setup_criminal(wanted_remaining=61)
        officer = await self._setup_police()

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 500000, 500000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 5000, 5000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_sys_msg.assert_called_once()
        self.assertIn("2 stars remaining", mock_sys_msg.call_args[0][1])

    async def test_expiry_no_1_star_message(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """When wanted drops from 2→0 in one tick, no intermediate 1-star message."""
        criminal = await self._setup_criminal(wanted_remaining=1.5)
        officer = await self._setup_police()

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 500000, 500000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 5000, 5000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(3):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_refresh.assert_called_once_with(criminal, mock_http_mod)

    # --- Star change → name refresh sync tests ---

    async def test_refresh_called_on_star_boundary(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """refresh_player_name is called exactly once when the star count crosses a boundary (5→4)."""
        criminal = await self._setup_criminal(wanted_remaining=181)
        officer = await self._setup_police()

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 500000, 500000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 5000, 5000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            # One tick: 181 → 180 (crosses 5→4 boundary)
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_refresh.assert_called_once_with(criminal, mock_http_mod)
        mock_sys_msg.assert_called_once()  # message and refresh in sync

    async def test_full_lifecycle_messages_and_refreshes_synced(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Full 300→0 countdown: 4 star transitions, each with both a message and a name refresh."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 500000, 500000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 5000, 5000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(300):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        # 4 star transitions: 5→4 (180), 4→3 (120), 3→2 (60), 2→0 (0)
        self.assertEqual(mock_sys_msg.call_count, 4)
        # Each star transition triggers refresh, and expiry is deduplicated
        self.assertEqual(mock_refresh.call_count, 4)

    async def test_no_refresh_for_offline_star_change(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Offline suspects don't get name refreshes even when their stars change."""
        await self._setup_criminal(wanted_remaining=181)
        officer = await self._setup_police()

        # Only officer online — criminal is offline
        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 500000, 500000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.criminals.get_players", new_callable=AsyncMock, return_value=players
        ):
            # Two ticks: 181 → 179 (crosses 5→4 boundary while offline)
            for _ in range(2):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_sys_msg.assert_not_called()
        mock_refresh.assert_not_called()
