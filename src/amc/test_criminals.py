"""Tests for the wanted countdown tick (amc.criminals)."""

from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone

from amc.criminals import (
    ESCAPE_DISTANCE,
    ESCAPE_FLOOR,
    ESCAPE_MESSAGE,
    _compute_stars,
    _last_escape_msg_sent,
    _last_star_notified,
    tick_wanted_countdown,
)
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


# ---------------------------------------------------------------------------
# Cop and criminal coordinates used across tests
# ---------------------------------------------------------------------------
# CLOSE: cop 10m from suspect (within ESCAPE_DISTANCE, max decay)
_SUSPECT_LOC = (5000, 5000, 0)
_COP_CLOSE    = (5000 + 1000, 5000, 0)   # 1000 units = 10m (MIN_DISTANCE)
_COP_MED      = (5000 + 5000, 5000, 0)   # 5000 units = 50m (REF_DISTANCE, decay=1.0)
_COP_ESCAPED  = (5000 + ESCAPE_DISTANCE + 1000, 5000, 0)  # > 200m away
_COP_FAR      = (5000 + 100_000, 5000, 0)  # 1000m — well beyond escape distance


class ComputeStarsTests(TestCase):
    """Unit tests for _compute_stars helper.

    LEVEL_PER_STAR = 60, so:
      5 stars: wanted_remaining > 240   (241–300)
      4 stars: wanted_remaining 181–240
      3 stars: wanted_remaining 121–180
      2 stars: wanted_remaining 61–120
      1 star:  wanted_remaining 1–60
      0 stars: wanted_remaining <= 0
    """

    def test_300_is_5_stars(self):
        self.assertEqual(_compute_stars(300), 5)

    def test_241_is_5_stars(self):
        self.assertEqual(_compute_stars(241), 5)

    def test_240_is_4_stars(self):
        self.assertEqual(_compute_stars(240), 4)

    def test_181_is_4_stars(self):
        self.assertEqual(_compute_stars(181), 4)

    def test_180_is_3_stars(self):
        self.assertEqual(_compute_stars(180), 3)

    def test_121_is_3_stars(self):
        self.assertEqual(_compute_stars(121), 3)

    def test_120_is_2_stars(self):
        self.assertEqual(_compute_stars(120), 2)

    def test_61_is_2_stars(self):
        self.assertEqual(_compute_stars(61), 2)

    def test_60_is_1_star(self):
        self.assertEqual(_compute_stars(60), 1)

    def test_1_is_1_star(self):
        self.assertEqual(_compute_stars(1), 1)

    def test_0_is_0_stars(self):
        self.assertEqual(_compute_stars(0), 0)

    def test_negative_is_0_stars(self):
        self.assertEqual(_compute_stars(-10), 0)

    def test_floor_is_0_stars(self):
        """ESCAPE_FLOOR (0.1) is below 1 star threshold."""
        self.assertEqual(_compute_stars(ESCAPE_FLOOR), 1)


@patch("amc.criminals.refresh_player_name", new_callable=AsyncMock)
@patch("amc.criminals.send_system_message", new_callable=AsyncMock)
class WantedCountdownTickTests(TestCase):
    """Integration tests for tick_wanted_countdown."""

    def setUp(self):
        _last_star_notified.clear()
        _last_escape_msg_sent.clear()

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

    # -----------------------------------------------------------------------
    # No decay without police/online
    # -----------------------------------------------------------------------

    async def test_no_cops_online_no_decay(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Wanted persists when no cops are on duty — no decay at all."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 300)
        self.assertIsNone(wanted.expired_at)
        mock_sys_msg.assert_not_called()
        mock_refresh.assert_not_called()

    async def test_offline_suspect_no_decay(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Offline suspects don't decay even when cops are online."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        # Only officer is online — criminal not in player list
        players = _make_players_list(
            [_make_player_data(officer.player.unique_id, officer.guid, *_COP_CLOSE)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 300)
        self.assertIsNone(wanted.expired_at)
        mock_sys_msg.assert_not_called()
        mock_refresh.assert_not_called()

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

    # -----------------------------------------------------------------------
    # Proximity decay — near police
    # -----------------------------------------------------------------------

    async def test_close_cop_fast_decay(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Cop at MIN_DISTANCE (10m) causes MAX_DECAY per tick, clamped at floor."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_CLOSE
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(300):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        # Decays fast but clamps at ESCAPE_FLOOR — cannot expire while cop is close
        self.assertAlmostEqual(wanted.wanted_remaining, ESCAPE_FLOOR, places=5)
        self.assertIsNone(wanted.expired_at)

    async def test_near_cop_clamps_at_floor(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Wanted cannot drop below ESCAPE_FLOOR while police are within ESCAPE_DISTANCE."""
        criminal = await self._setup_criminal(wanted_remaining=1.0)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_CLOSE
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(5):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertAlmostEqual(wanted.wanted_remaining, ESCAPE_FLOOR, places=5)
        self.assertIsNone(wanted.expired_at)

    async def test_medium_cop_normal_decay_rate(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Cop at REF_DISTANCE (50m) gives decay_rate ~1.0/tick."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        # At 50m (REF_DISTANCE), decay_rate = 1.0 → 10 ticks → ~290
        self.assertAlmostEqual(wanted.wanted_remaining, 290.0, delta=1.0)

    # -----------------------------------------------------------------------
    # Escape-gate clearing
    # -----------------------------------------------------------------------

    async def test_escaped_at_floor_clears_instantly(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """When suspect escapes beyond ESCAPE_DISTANCE with wanted at floor, wanted expires."""
        criminal = await self._setup_criminal(wanted_remaining=ESCAPE_FLOOR)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_ESCAPED
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)

    async def test_escaped_above_floor_no_decay(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Suspect beyond ESCAPE_DISTANCE with wanted above floor: no decay, wanted persists.

        Suspect must return to police range, let 1/r² decay bring it to floor,
        then escape again to clear.
        """
        criminal = await self._setup_criminal(wanted_remaining=60)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_ESCAPED
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 60)  # unchanged
        self.assertIsNone(wanted.expired_at)

    async def test_full_lifecycle_near_then_escape(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Full lifecycle: 300→floor near police, then suspect escapes and wanted clears."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # 50m — within escape zone

        # Phase 1: suspect near police, decays to floor
        near_players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=near_players):
            for _ in range(310):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertAlmostEqual(wanted.wanted_remaining, ESCAPE_FLOOR, places=5)
        self.assertIsNone(wanted.expired_at)

        # Phase 2: suspect escapes → wanted clears
        ex, ey, ez = _COP_ESCAPED
        escaped_players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, ex, ey, ez),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=escaped_players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)

    # -----------------------------------------------------------------------
    # Escape popup messages
    # -----------------------------------------------------------------------

    async def test_escape_popup_sent_when_at_floor_near_police(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Escape popup is sent when suspect reaches floor while near police."""
        criminal = await self._setup_criminal(wanted_remaining=ESCAPE_FLOOR)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_CLOSE  # within ESCAPE_DISTANCE
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        # Find the escape message call among all send_system_message calls
        escape_calls = [
            c for c in mock_sys_msg.call_args_list
            if len(c.args) > 1 and c.args[1] == ESCAPE_MESSAGE
        ]
        self.assertEqual(len(escape_calls), 1)
        self.assertEqual(escape_calls[0].kwargs["character_guid"], criminal.guid)

    async def test_escape_popup_throttled(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Escape popup is not sent again within ESCAPE_MSG_COOLDOWN seconds."""
        criminal = await self._setup_criminal(wanted_remaining=ESCAPE_FLOOR)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_CLOSE
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(5):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        # Should only have been sent once (cooldown not yet elapsed)
        escape_calls = [
            c for c in mock_sys_msg.call_args_list
            if len(c.args) > 1 and c.args[1] == ESCAPE_MESSAGE
        ]
        self.assertEqual(len(escape_calls), 1)

    async def test_escape_popup_not_sent_when_not_at_floor(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Escape popup is not sent while wanted_remaining is still above floor."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # 50m — within ESCAPE_DISTANCE, slow decay
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        # Tick only a few times — wanted well above floor still
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(3):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        escape_calls = [
            c for c in mock_sys_msg.call_args_list
            if len(c.args) > 1 and c.args[1] == ESCAPE_MESSAGE
        ]
        self.assertEqual(len(escape_calls), 0)

    async def test_escape_msg_state_cleaned_on_expiry(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """_last_escape_msg_sent is cleaned up when wanted expires."""
        criminal = await self._setup_criminal(wanted_remaining=ESCAPE_FLOOR)
        officer = await self._setup_police()

        import time
        _last_escape_msg_sent[criminal.guid] = time.monotonic()

        sx, sy, sz = _SUSPECT_LOC
        ex, ey, ez = _COP_ESCAPED
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, ex, ey, ez),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        self.assertNotIn(criminal.guid, _last_escape_msg_sent)

    # -----------------------------------------------------------------------
    # Star transitions and name refresh
    # -----------------------------------------------------------------------

    async def test_star_change_sends_message(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Crossing a star boundary sends the corresponding message to the suspect."""
        # 241 = 5 stars; one tick at REF_DISTANCE (1.0/tick) → 240 = 4 stars
        criminal = await self._setup_criminal(wanted_remaining=241)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # REF_DISTANCE = 1.0/tick
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        star_calls = [
            c for c in mock_sys_msg.call_args_list
            if len(c.args) > 1 and "4 stars remaining" in c.args[1]
        ]
        self.assertEqual(len(star_calls), 1)
        self.assertEqual(star_calls[0].kwargs["character_guid"], criminal.guid)

    async def test_star_change_refreshes_name(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """refresh_player_name is called when the star count changes."""
        criminal = await self._setup_criminal(wanted_remaining=241)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_refresh.assert_called_once_with(criminal, mock_http_mod)

    async def test_no_message_when_star_unchanged(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """No star-change message sent if wanted decays without crossing a boundary."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # 1.0/tick, 300 → 299 (still 5 stars)
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        star_calls = [
            c for c in mock_sys_msg.call_args_list
            if len(c.args) > 1 and c.args[1] != ESCAPE_MESSAGE
        ]
        self.assertEqual(len(star_calls), 0)

    async def test_last_star_notified_cleaned_on_expiry(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """_last_star_notified entry is removed when wanted expires."""
        criminal = await self._setup_criminal(wanted_remaining=ESCAPE_FLOOR)
        officer = await self._setup_police()
        _last_star_notified[criminal.guid] = 1

        sx, sy, sz = _SUSPECT_LOC
        ex, ey, ez = _COP_ESCAPED
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, ex, ey, ez),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        self.assertNotIn(criminal.guid, _last_star_notified)

    async def test_expiry_refreshes_player_name(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """refresh_player_name is called when wanted expires."""
        criminal = await self._setup_criminal(wanted_remaining=ESCAPE_FLOOR)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        ex, ey, ez = _COP_ESCAPED
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, ex, ey, ez),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_refresh.assert_called_once_with(criminal, mock_http_mod)
