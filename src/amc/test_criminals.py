"""Tests for the wanted countdown tick (amc.criminals).

Hybrid mechanic:
  - Online suspects always decay at BASE_DECAY_PER_TICK (1.0/tick).
    Clears in BASE_WANTED_DURATION (5 min = 300 ticks) with no police.
  - Police proximity SLOWS decay via 1/r² law:
      effective_decay = BASE_DECAY_PER_TICK / (1 + proximity_factor)
    Closer police → larger factor → slower decay. Decay never reverses.
  - Escape gate: cannot expire while any cop is within ESCAPE_DISTANCE (200m).
    Clamped at ESCAPE_FLOOR. Clears freely once all cops are beyond 200m.
  - Offline suspects: no decay, wanted persists indefinitely.
"""

import time
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone

from amc.criminals import (
    BASE_DECAY_PER_TICK,
    BASE_WANTED_DURATION,
    BOUNTY_GROWTH_PER_TICK,
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
_COP_CLOSE    = (5000 + 1000, 5000, 0)    # 1000 units = 10m  (MIN_DISTANCE → factor=10)
_COP_MED      = (5000 + 10_000, 5000, 0)  # 10_000 units = 100m (REF_DISTANCE → factor=1)
_COP_ESCAPED  = (5000 + ESCAPE_DISTANCE + 1000, 5000, 0)  # > ESCAPE_DISTANCE away
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
    # Base time-based decay — always ticks without police
    # -----------------------------------------------------------------------

    async def test_no_cops_online_decays_at_base_rate(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Online suspect decays at BASE_DECAY_PER_TICK even with no police online."""
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
        # 10 ticks × 1.0/tick = 10 units decay → 290
        self.assertAlmostEqual(wanted.wanted_remaining, 300 - 10 * BASE_DECAY_PER_TICK, delta=0.1)
        self.assertIsNone(wanted.expired_at)

    async def test_no_cops_online_expires_after_base_duration(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Wanted expires after BASE_WANTED_DURATION ticks with no police."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(BASE_WANTED_DURATION):
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
    # Police proximity — slows decay (1/r²)
    # -----------------------------------------------------------------------

    async def test_close_cop_decays_slower_than_no_police(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Close cop slows decay: wanted_remaining is higher with cop than without."""
        # Criminal A: cop nearby (slows decay)
        criminal_a = await self._setup_criminal(wanted_remaining=200)
        officer = await self._setup_police()

        # Criminal B: no cop (full decay rate)
        criminal_b = await self._setup_criminal(wanted_remaining=200)

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_CLOSE  # 10m → factor=10 → effective_decay = 1.0/(1+10) ≈ 0.09/tick

        # Scenario A: cop at 10m slows decay
        players_a = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal_a.player.unique_id, criminal_a.guid, sx, sy, sz),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_a):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)
        wanted_a = await Wanted.objects.aget(character=criminal_a)

        # Scenario B: no cops — full decay
        players_b = _make_players_list([
            _make_player_data(criminal_b.player.unique_id, criminal_b.guid, sx, sy, sz),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_b):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)
        wanted_b = await Wanted.objects.aget(character=criminal_b)

        # Cop nearby → slower decay → higher remaining
        self.assertGreater(wanted_a.wanted_remaining, wanted_b.wanted_remaining)

    async def test_close_cop_decays_slower_than_medium_cop(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Closer cop = more slowing: 10m decays slower than 100m."""
        criminal_close = await self._setup_criminal(wanted_remaining=200)
        officer_close = await self._setup_police()

        criminal_med = await self._setup_criminal(wanted_remaining=200)
        officer_med = await self._setup_police()

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        sx, sy, sz = _SUSPECT_LOC

        # Close cop (10m): factor=10, effective_decay ≈ 0.09/tick
        players_close = _make_players_list([
            _make_player_data(officer_close.player.unique_id, officer_close.guid, *_COP_CLOSE),
            _make_player_data(criminal_close.player.unique_id, criminal_close.guid, sx, sy, sz),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_close):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)
        wanted_close = await Wanted.objects.aget(character=criminal_close)

        # Medium cop (100m): factor=1, effective_decay = 0.5/tick
        players_med = _make_players_list([
            _make_player_data(officer_med.player.unique_id, officer_med.guid, *_COP_MED),
            _make_player_data(criminal_med.player.unique_id, criminal_med.guid, sx, sy, sz),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_med):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)
        wanted_med = await Wanted.objects.aget(character=criminal_med)

        # Closer cop → slower decay → higher remaining
        self.assertGreater(wanted_close.wanted_remaining, wanted_med.wanted_remaining)

    async def test_med_cop_decays_at_half_base_rate(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Cop at REF_DISTANCE (100m): factor=1.0, effective_decay = 0.5/tick."""
        criminal = await self._setup_criminal(wanted_remaining=200)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # 100m → factor=1.0 → decay = 1.0/(1+1) = 0.5/tick
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
        # 10 ticks × 0.5/tick = 5 decay → 195
        self.assertAlmostEqual(wanted.wanted_remaining, 195.0, delta=0.5)
        self.assertIsNone(wanted.expired_at)

    async def test_cop_beyond_escape_distance_uses_full_rate(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Cop beyond ESCAPE_DISTANCE: no proximity effect, full BASE_DECAY_PER_TICK applies."""
        criminal = await self._setup_criminal(wanted_remaining=200)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_ESCAPED  # > 200m
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
        # Full decay: 10 × 1.0 = 10 units → 190
        self.assertAlmostEqual(wanted.wanted_remaining, 190.0, delta=0.5)
        self.assertIsNone(wanted.expired_at)

    async def test_bounty_grows_near_police(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Bounty (amount) grows proportionally to police proximity."""
        criminal = await self._setup_criminal(wanted_remaining=200)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # 100m → factor=1.0
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
        # factor=1.0 at 100m, 5 ticks → amount += 5 × BOUNTY_GROWTH_PER_TICK
        self.assertGreaterEqual(wanted.amount, 5 * BOUNTY_GROWTH_PER_TICK)

    async def test_bounty_does_not_grow_without_police(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Bounty (amount) stays at 0 when no police are nearby."""
        criminal = await self._setup_criminal(wanted_remaining=200)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(5):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.amount, 0)

    # -----------------------------------------------------------------------
    # Escape gate — cannot expire while near police
    # -----------------------------------------------------------------------

    async def test_near_cop_clamps_at_escape_floor(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Wanted cannot drop below ESCAPE_FLOOR while police are within ESCAPE_DISTANCE."""
        criminal = await self._setup_criminal(wanted_remaining=1.0)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # within 200m
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
        self.assertGreaterEqual(wanted.wanted_remaining, ESCAPE_FLOOR)
        self.assertIsNone(wanted.expired_at)

    async def test_cannot_expire_near_police(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Wanted at ESCAPE_FLOOR near police never expires."""
        criminal = await self._setup_criminal(wanted_remaining=ESCAPE_FLOOR)
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
            for _ in range(20):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, ESCAPE_FLOOR)
        self.assertIsNone(wanted.expired_at)

    async def test_beyond_escape_distance_expires_normally(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Suspect beyond ESCAPE_DISTANCE decays at full rate and can expire."""
        criminal = await self._setup_criminal(wanted_remaining=5.0)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        ex, ey, ez = _COP_ESCAPED  # > 200m
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, ex, ey, ez),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):  # 10 × 1.0/tick = 10 > 5 → expires
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)

    async def test_full_lifecycle_near_then_escape(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Full lifecycle: runs down near police (slowed) then expires quickly once beyond 200m."""
        # With INITIAL=300, cop at 100m → 0.5/tick, so from 300 needs 600 ticks to clear
        # Start low to keep test fast
        criminal = await self._setup_criminal(wanted_remaining=2.0)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # 100m → 0.5/tick → 4 ticks to drain 2.0 to floor

        near_players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        # Phase 1: near police — slows down, floors at ESCAPE_FLOOR
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=near_players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, ESCAPE_FLOOR)
        self.assertIsNone(wanted.expired_at)

        # Phase 2: escape beyond ESCAPE_DISTANCE → full rate, clears in 1 tick
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
        """Escape popup is sent when suspect heat is clamped at ESCAPE_FLOOR near police."""
        criminal = await self._setup_criminal(wanted_remaining=ESCAPE_FLOOR)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, *_COP_MED),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
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
        criminal = await self._setup_criminal(wanted_remaining=ESCAPE_FLOOR)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, *_COP_MED),
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
        cx, cy, cz = _COP_MED
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
        criminal = await self._setup_criminal(wanted_remaining=0.5)
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

        No police: decay = 1.0/tick.
        Start at 241 (W5). After 2 ticks: 241 - 2.0 = 239 (W4).
        """
        criminal = await self._setup_criminal(wanted_remaining=241)

        sx, sy, sz = _SUSPECT_LOC
        # No police — full decay of 1.0/tick
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
        # No police — 300 - 1.0 = 299 (still W5)
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
        criminal = await self._setup_criminal(wanted_remaining=0.5)
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
        criminal = await self._setup_criminal(wanted_remaining=0.5)
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
