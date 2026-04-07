"""Tests for the wanted countdown tick (amc.criminals).

New mechanic (replaced):
  - wanted_remaining decays at BASE_DECAY_PER_TICK (0.5/tick) every tick.
  - Police within ESCAPE_DISTANCE (500m) ADD heat (1/r²), prolonging the Wanted.
  - Escape gate: cannot expire while any officer is within ESCAPE_DISTANCE.
  - Offline suspects: no decay, wanted persists.
"""

import time
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone

from amc.criminals import (
    BASE_DECAY_PER_TICK,
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
_SUSPECT_LOC = (5000, 5000, 0)
_COP_CLOSE    = (5000 + 1000, 5000, 0)    # 1000 units = 10m (MIN_DISTANCE)
_COP_MED      = (5000 + 10_000, 5000, 0)  # 10_000 units = 100m (REF_DISTANCE)
_COP_ESCAPED  = (5000 + ESCAPE_DISTANCE + 1000, 5000, 0)  # > 500m away
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

    def test_floor_is_1_star(self):
        """ESCAPE_FLOOR (0.1) is above 0 so still counts as 1 star."""
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
    # Time-based decay — without police
    # -----------------------------------------------------------------------

    async def test_no_cops_online_still_decays(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Online suspect decays at BASE_DECAY_PER_TICK even without any police."""
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
        # 10 ticks × 0.5/tick = 5 units decay → 295
        self.assertAlmostEqual(wanted.wanted_remaining, 300 - 10 * BASE_DECAY_PER_TICK, delta=0.1)
        self.assertIsNone(wanted.expired_at)

    async def test_no_cops_online_expires_after_600_ticks(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Wanted expires naturally after 600 ticks (10 min) with no police."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(600):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)

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
    # Police proximity — prolongs (adds heat)
    # -----------------------------------------------------------------------

    async def test_close_cop_adds_heat_prolongs_wanted(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Cop at MIN_DISTANCE (10m) adds MAX_DECAY heat/tick, net growth is positive."""
        criminal = await self._setup_criminal(wanted_remaining=150)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_CLOSE  # 10m → factor=10 → growth = 10.0/tick, decay = 0.5/tick → net +9.5/tick
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
        # Net growth ~+9.5/tick × 5 ticks = +47.5, so wanted grew from 150 toward 197.5
        # But capped at INITIAL_WANTED_LEVEL (300). Either way it should be > 150.
        self.assertGreater(wanted.wanted_remaining, 150)
        self.assertIsNone(wanted.expired_at)

    async def test_close_cop_clamps_at_initial_level(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Police cannot push heat above INITIAL_WANTED_LEVEL (300)."""
        criminal = await self._setup_criminal(wanted_remaining=299)
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
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertLessEqual(wanted.wanted_remaining, Wanted.INITIAL_WANTED_LEVEL)
        self.assertIsNone(wanted.expired_at)

    async def test_near_cop_clamps_at_escape_floor(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Wanted cannot drop below ESCAPE_FLOOR while police are within ESCAPE_DISTANCE."""
        criminal = await self._setup_criminal(wanted_remaining=1.0)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_CLOSE  # point-blank — massive growth > tiny decay → stays well above floor
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
        self.assertGreaterEqual(wanted.wanted_remaining, ESCAPE_FLOOR)
        self.assertIsNone(wanted.expired_at)

    async def test_medium_cop_slows_decay(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Cop at REF_DISTANCE (100m) adds 1.0/tick heat vs 0.5/tick decay → net +0.5/tick."""
        criminal = await self._setup_criminal(wanted_remaining=150)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # 100m → factor=1.0 → growth=1.0, decay=0.5, net=+0.5/tick
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
        # Net +0.5/tick × 10 = +5, so wanted_remaining ≈ 155
        self.assertAlmostEqual(wanted.wanted_remaining, 155.0, delta=1.0)

    async def test_escaped_cop_decays_normally(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Cop beyond ESCAPE_DISTANCE: no proximity growth, only natural decay applies."""
        criminal = await self._setup_criminal(wanted_remaining=150)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_ESCAPED  # > 500m
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
        # Only BASE_DECAY_PER_TICK applies: 10 × 0.5 = 5 decay → 145
        self.assertAlmostEqual(wanted.wanted_remaining, 145.0, delta=0.5)
        self.assertIsNone(wanted.expired_at)

    # -----------------------------------------------------------------------
    # Escape gate — cannot expire while near police
    # -----------------------------------------------------------------------

    async def test_cannot_expire_near_police(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Wanted cannot expire (floor at ESCAPE_FLOOR) while police are within ESCAPE_DISTANCE."""
        # Give very low starting heat so it would naturally hit 0 quickly
        criminal = await self._setup_criminal(wanted_remaining=1.0)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # 100m — within ESCAPE_DISTANCE, net +0.5/tick
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(20):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertGreater(wanted.wanted_remaining, 0)
        self.assertIsNone(wanted.expired_at)

    async def test_escaped_beyond_500m_expires_naturally(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Suspect beyond ESCAPE_DISTANCE: natural decay only, can expire freely."""
        criminal = await self._setup_criminal(wanted_remaining=3.0)  # 6 ticks at 0.5/tick
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_ESCAPED  # > 500m
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
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)

    async def test_full_lifecycle_near_then_escape(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Full lifecycle: near police (wanted grows/floors), then escapes and expires."""
        criminal = await self._setup_criminal(wanted_remaining=1.0)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # 100m — net growth, floors at ESCAPE_FLOOR

        near_players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        # Phase 1: near police — wanted persists (floored at ESCAPE_FLOOR or grows)
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=near_players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertIsNone(wanted.expired_at)

        # Phase 2: force heat to floor then escape → wanted clears
        await Wanted.objects.filter(character=criminal).aupdate(wanted_remaining=ESCAPE_FLOOR)

        ex, ey, ez = _COP_ESCAPED
        escaped_players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, ex, ey, ez),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=escaped_players):
            for _ in range(2):
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
        """Escape popup is sent when suspect heat floors at ESCAPE_FLOOR near police."""
        # Use a very low starting heat with a cop far enough that decay > growth
        # cop at 300m: factor ≈ 0.11, growth ≈ 0.11/tick < decay 0.5/tick → heat decays to floor
        _COP_300M = (5000 + 30_000, 5000, 0)  # 300m
        criminal = await self._setup_criminal(wanted_remaining=ESCAPE_FLOOR + 0.2)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, *_COP_300M),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(5):
                await tick_wanted_countdown(mock_http, mock_http_mod)

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
        _COP_300M = (5000 + 30_000, 5000, 0)
        criminal = await self._setup_criminal(wanted_remaining=ESCAPE_FLOOR)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, *_COP_300M),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(5):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        escape_calls = [
            c for c in mock_sys_msg.call_args_list
            if len(c.args) > 1 and c.args[1] == ESCAPE_MESSAGE
        ]
        self.assertEqual(len(escape_calls), 1)

    async def test_escape_popup_not_sent_when_above_floor(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Escape popup is not sent while wanted_remaining is well above floor."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # net +0.5/tick at 100m — heat growing, not at floor
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

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
        criminal = await self._setup_criminal(wanted_remaining=0.4)
        officer = await self._setup_police()

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
            for _ in range(2):
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
        """Crossing a star boundary (5→4) sends the corresponding message to the suspect.

        With no police: decay = 0.5/tick.
        Start at 241 (W5). After 2 ticks: 241 - 1.0 = 240 → W4.
        """
        criminal = await self._setup_criminal(wanted_remaining=241)

        sx, sy, sz = _SUSPECT_LOC
        # No police — pure decay of 0.5/tick
        players = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(2):
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

        sx, sy, sz = _SUSPECT_LOC
        players = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(2):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_refresh.assert_called_once_with(criminal, mock_http_mod)

    async def test_no_message_when_star_unchanged(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """No star-change message sent if wanted decays without crossing a boundary."""
        criminal = await self._setup_criminal(wanted_remaining=300)

        sx, sy, sz = _SUSPECT_LOC
        # No police — 300 → 299.5 (still W5), no boundary crossed
        players = _make_players_list([
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
        criminal = await self._setup_criminal(wanted_remaining=0.4)
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
            for _ in range(2):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        self.assertNotIn(criminal.guid, _last_star_notified)

    async def test_expiry_refreshes_player_name(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """refresh_player_name is called when wanted expires."""
        criminal = await self._setup_criminal(wanted_remaining=0.4)
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
            for _ in range(2):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_refresh.assert_called_once_with(criminal, mock_http_mod)
