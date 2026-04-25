"""Tests for the auto-arrest patrol loop (amc.auto_arrest)."""

from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from amc.auto_arrest import _STILL_TICKS, _is_wanted, _patrol_tick
from amc.factories import CharacterFactory, PlayerFactory
from amc.models import Confiscation, PoliceSession, TeleportPoint, Wanted

from django.contrib.gis.geos import Point


class _EmptyAsyncIter:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _make_player_data(unique_id, character_guid, x, y, z, vehicle=None):
    """Build a fake player dict matching the game server /player/list format.

    The game server always includes a ``vehicle`` key — it is a dict
    when the player is in a vehicle, and ``None`` when on foot.
    """
    return {
        "unique_id": str(unique_id),
        "character_guid": character_guid,
        "location": f"X={x} Y={y} Z={z}",
        "vehicle": vehicle,
    }


def _make_players_list(player_datas):
    """Wrap player datas into the format returned by get_players()."""
    return [(d["unique_id"], d) for d in player_datas]


@patch("amc.commands.faction.announce", new_callable=AsyncMock)
@patch("amc.commands.faction.send_system_message", new_callable=AsyncMock)
@patch("amc.commands.faction.on_player_profit", new_callable=AsyncMock)
@patch("amc.commands.faction.refresh_player_name", new_callable=AsyncMock)
@patch(
    "amc.commands.faction.get_active_police_characters",
    new_callable=AsyncMock,
    return_value=_EmptyAsyncIter(),
)
@patch("amc.commands.faction.record_confiscation_for_level", new_callable=AsyncMock)
@patch(
    "amc.commands.faction.record_treasury_confiscation_income", new_callable=AsyncMock
)
@patch("amc.commands.faction.transfer_money", new_callable=AsyncMock)
@patch("amc.commands.faction.teleport_player", new_callable=AsyncMock)
@patch("amc.commands.faction.force_exit_vehicle", new_callable=AsyncMock)
@patch("amc.commands.faction.show_popup", new_callable=AsyncMock)
class AutoArrestPatrolTests(TestCase):
    """Tests for _patrol_tick auto-arrest behavior."""

    def setUp(self):
        cache.clear()

    async def _setup_world(self):
        """Create jail teleport point, officer, and criminal."""
        # Create jail teleport point
        await TeleportPoint.objects.acreate(
            name="jail",
            location=Point(0, 0, 0),
        )

        # Create officer with active police session
        officer_player = await sync_to_async(PlayerFactory)()
        officer = await sync_to_async(CharacterFactory)(player=officer_player)
        officer.last_online = timezone.now()
        await officer.asave(update_fields=["last_online"])
        await PoliceSession.objects.acreate(character=officer)

        # Create criminal
        criminal_player = await sync_to_async(PlayerFactory)()
        criminal = await sync_to_async(CharacterFactory)(player=criminal_player)
        criminal.last_online = timezone.now()
        await criminal.asave(update_fields=["last_online"])

        return officer, criminal

    async def test_auto_arrests_wanted_suspect(
        self,
        mock_popup,
        mock_exit_vehicle,
        mock_teleport,
        mock_transfer,
        mock_treasury,
        mock_level,
        mock_get_active,
        mock_refresh,
        mock_on_profit,
        mock_sys_msg,
        mock_announce,
    ):
        """Patrol tick arrests a suspect with active Wanted after enough still ticks."""
        officer, criminal = await self._setup_world()

        # Criminal has active Wanted status
        await Wanted.objects.acreate(
            character=criminal,
            wanted_remaining=300,
        )

        # Both players near each other (within 50m = 5000 game units)
        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 1000, 1000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 1500, 1000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        # Must accumulate enough still ticks before arrest triggers
        prev_locations = {}
        still_counters = {}
        with patch(
            "amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(_STILL_TICKS):
                prev_locations, still_counters = await _patrol_tick(
                    mock_http, mock_http_mod, prev_locations, still_counters
                )

        # Criminal should have been teleported to jail
        mock_teleport.assert_called_once()
        # Popup shown
        mock_popup.assert_called_once()
        # Confiscation record created
        self.assertEqual(await Confiscation.objects.acount(), 1)
        conf = await Confiscation.objects.afirst()
        self.assertEqual(conf.officer_id, officer.id)
        self.assertEqual(conf.character_id, criminal.id)

    async def test_no_arrest_without_wanted_status(
        self,
        mock_popup,
        mock_exit_vehicle,
        mock_teleport,
        mock_transfer,
        mock_treasury,
        mock_level,
        mock_get_active,
        mock_refresh,
        mock_on_profit,
        mock_sys_msg,
        mock_announce,
    ):
        """Patrol tick does NOT arrest suspects without Wanted status."""
        officer, criminal = await self._setup_world()

        # Criminal has NO Wanted status
        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 1000, 1000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 1500, 1000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players
        ):
            prev = {}
            sc = {}
            for _ in range(_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        mock_teleport.assert_not_called()
        self.assertEqual(await Confiscation.objects.acount(), 0)

    async def test_no_arrest_when_out_of_range(
        self,
        mock_popup,
        mock_exit_vehicle,
        mock_teleport,
        mock_transfer,
        mock_treasury,
        mock_level,
        mock_get_active,
        mock_refresh,
        mock_on_profit,
        mock_sys_msg,
        mock_announce,
    ):
        """Patrol tick does NOT arrest suspects who are too far from police."""
        officer, criminal = await self._setup_world()

        await Wanted.objects.acreate(
            character=criminal,
            wanted_remaining=300,
        )

        # Criminal is 100m away (10000 game units > 1500 auto-arrest radius on foot)
        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 1000, 1000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 11000, 1000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players
        ):
            prev = {}
            sc = {}
            for _ in range(_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        mock_teleport.assert_not_called()

    async def test_police_not_arrested(
        self,
        mock_popup,
        mock_exit_vehicle,
        mock_teleport,
        mock_transfer,
        mock_treasury,
        mock_level,
        mock_get_active,
        mock_refresh,
        mock_on_profit,
        mock_sys_msg,
        mock_announce,
    ):
        """Police officers are never auto-arrested, even if nearby each other."""
        officer, _ = await self._setup_world()

        # Create a second officer nearby
        officer2_player = await sync_to_async(PlayerFactory)()
        officer2 = await sync_to_async(CharacterFactory)(player=officer2_player)
        officer2.last_online = timezone.now()
        await officer2.asave(update_fields=["last_online"])
        await PoliceSession.objects.acreate(character=officer2)

        # Even if officer2 has Wanted status (shouldn't happen, but edge case)
        await Wanted.objects.acreate(
            character=officer2,
            wanted_remaining=300,
        )

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 1000, 1000, 0
                ),
                _make_player_data(
                    officer2.player.unique_id, officer2.guid, 1500, 1000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players
        ):
            prev = {}
            sc = {}
            for _ in range(_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        mock_teleport.assert_not_called()

    async def test_speed_check_blocks_fast_suspects(
        self,
        mock_popup,
        mock_exit_vehicle,
        mock_teleport,
        mock_transfer,
        mock_treasury,
        mock_level,
        mock_get_active,
        mock_refresh,
        mock_on_profit,
        mock_sys_msg,
        mock_announce,
    ):
        """Suspects moving too fast are NOT auto-arrested (speed > 30 km/h)."""
        officer, criminal = await self._setup_world()

        await Wanted.objects.acreate(
            character=criminal,
            wanted_remaining=300,
        )

        from amc.commands.faction import _build_player_locations

        # Simulate continuous fast movement: 600 units per tick at 0.5s = 1200 u/s > 556 u/s limit
        # Build prev_locations for the first tick
        prev_criminal_data = _make_player_data(
            criminal.player.unique_id, criminal.guid, 1500, 1000, 0, vehicle="truck"
        )
        prev_locations = _build_player_locations(
            [(prev_criminal_data["unique_id"], prev_criminal_data)]
        )

        # Each call to get_players returns suspect shifted 600 units further
        tick_counter = [0]

        def make_players_for_tick(*args, **kwargs):
            tick_counter[0] += 1
            x = 1500 + 600 * tick_counter[0]
            return _make_players_list(
                [
                    _make_player_data(
                        officer.player.unique_id, officer.guid, x, 1000, 0
                    ),
                    _make_player_data(
                        criminal.player.unique_id,
                        criminal.guid,
                        x,
                        1000,
                        0,
                        vehicle="truck",
                    ),
                ]
            )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        sc = {}
        with patch(
            "amc.auto_arrest.get_players",
            new_callable=AsyncMock,
            side_effect=make_players_for_tick,
        ):
            for _ in range(_STILL_TICKS + 2):
                prev_locations, sc = await _patrol_tick(
                    mock_http, mock_http_mod, prev_locations, sc
                )

        mock_teleport.assert_not_called()

    async def test_expired_wanted_not_arrested(
        self,
        mock_popup,
        mock_exit_vehicle,
        mock_teleport,
        mock_transfer,
        mock_treasury,
        mock_level,
        mock_get_active,
        mock_refresh,
        mock_on_profit,
        mock_sys_msg,
        mock_announce,
    ):
        """Wanted with wanted_remaining=0 does not trigger arrest."""
        officer, criminal = await self._setup_world()

        # Wanted with zero protection remaining
        await Wanted.objects.acreate(
            character=criminal,
            wanted_remaining=0,
        )

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 1000, 1000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 1500, 1000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players
        ):
            prev = {}
            sc = {}
            for _ in range(_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        mock_teleport.assert_not_called()

    async def test_server_announcement_on_arrest(
        self,
        mock_popup,
        mock_exit_vehicle,
        mock_teleport,
        mock_transfer,
        mock_treasury,
        mock_level,
        mock_get_active,
        mock_refresh,
        mock_on_profit,
        mock_sys_msg,
        mock_announce,
    ):
        """Server announcement fires on auto-arrest after enough still ticks."""
        officer, criminal = await self._setup_world()

        await Wanted.objects.acreate(
            character=criminal,
            wanted_remaining=300,
        )

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 1000, 1000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 1500, 1000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        prev = {}
        sc = {}
        with patch(
            "amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        # announce() is called via asyncio.create_task, check it was called
        mock_announce.assert_called_once()
        call_args = mock_announce.call_args[0]
        self.assertIn(officer.name, call_args[0])
        # No confiscation (no Money deliveries) → no "confiscated" in message

    async def test_system_message_to_officer(
        self,
        mock_popup,
        mock_exit_vehicle,
        mock_teleport,
        mock_transfer,
        mock_treasury,
        mock_level,
        mock_get_active,
        mock_refresh,
        mock_on_profit,
        mock_sys_msg,
        mock_announce,
    ):
        """System message is sent to the officer on auto-arrest."""
        officer, criminal = await self._setup_world()

        await Wanted.objects.acreate(
            character=criminal,
            wanted_remaining=300,
        )

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 1000, 1000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 1500, 1000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        prev = {}
        sc = {}
        with patch(
            "amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        # System message to officer about auto-arrest
        mock_sys_msg.assert_called()
        # Find the call specifically to the officer
        officer_calls = [
            c
            for c in mock_sys_msg.call_args_list
            if c.kwargs.get("character_guid") == officer.guid
        ]
        self.assertTrue(len(officer_calls) >= 1)
        self.assertIn("auto-arrested", officer_calls[0][0][1])

    async def test_cooldown_prevents_rearrest(
        self,
        mock_popup,
        mock_exit_vehicle,
        mock_teleport,
        mock_transfer,
        mock_treasury,
        mock_level,
        mock_get_active,
        mock_refresh,
        mock_on_profit,
        mock_sys_msg,
        mock_announce,
    ):
        """After auto-arrest, suspect is NOT arrested again on subsequent ticks."""
        officer, criminal = await self._setup_world()

        await Wanted.objects.acreate(
            character=criminal,
            wanted_remaining=300,
        )

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 1000, 1000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 1500, 1000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        # Accumulate still ticks until arrest fires
        prev = {}
        sc = {}
        with patch(
            "amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        self.assertEqual(mock_teleport.call_count, 1)

        # Additional ticks: should NOT arrest again (Wanted already deleted)
        mock_teleport.reset_mock()
        with patch(
            "amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        mock_teleport.assert_not_called()

    async def test_single_tick_does_not_arrest(
        self,
        mock_popup,
        mock_exit_vehicle,
        mock_teleport,
        mock_transfer,
        mock_treasury,
        mock_level,
        mock_get_active,
        mock_refresh,
        mock_on_profit,
        mock_sys_msg,
        mock_announce,
    ):
        """A single tick is NOT enough to trigger auto-arrest."""
        officer, criminal = await self._setup_world()

        await Wanted.objects.acreate(
            character=criminal,
            wanted_remaining=300,
        )

        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id, officer.guid, 1000, 1000, 0
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 1500, 1000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch(
            "amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players
        ):
            await _patrol_tick(mock_http, mock_http_mod, {})

        # Should NOT have arrested on a single tick
        mock_teleport.assert_not_called()

    async def test_arrest_still_works_after_drifting_beyond_radius(
        self,
        mock_popup,
        mock_exit_vehicle,
        mock_teleport,
        mock_transfer,
        mock_treasury,
        mock_level,
        mock_get_active,
        mock_refresh,
        mock_on_profit,
        mock_sys_msg,
        mock_announce,
    ):
        """Suspect starting within radius but drifting beyond it slowly is still arrested.

        Radius is only checked on first contact. Subsequent ticks only enforce speed.
        """
        officer, criminal = await self._setup_world()

        await Wanted.objects.acreate(
            character=criminal,
            wanted_remaining=300,
        )

        # Tick 1: suspect starts within radius (500 units apart, well within 1500)
        # Ticks 2-5: suspect drifts slowly beyond radius (200 units per tick = 400 u/s < 556 limit)
        tick_counter = [0]

        def make_drifting_players(*args, **kwargs):
            tick_counter[0] += 1
            # Suspect starts at 500 units from officer, drifts 200u per tick
            sus_x = 1000 + 500 + 200 * (tick_counter[0] - 1)
            return _make_players_list(
                [
                    _make_player_data(
                        officer.player.unique_id, officer.guid, 1000, 1000, 0
                    ),
                    _make_player_data(
                        criminal.player.unique_id, criminal.guid, sus_x, 1000, 0
                    ),
                ]
            )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        prev = {}
        sc = {}
        with patch(
            "amc.auto_arrest.get_players",
            new_callable=AsyncMock,
            side_effect=make_drifting_players,
        ):
            for _ in range(_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        # By tick 5, suspect is at 1500+800=2300 units away — beyond the 1500 radius
        # But arrest should still fire because radius was only checked on tick 1
        mock_teleport.assert_called_once()

    async def test_auto_arrest_with_police_vehicle(
        self,
        mock_popup,
        mock_exit_vehicle,
        mock_teleport,
        mock_transfer,
        mock_treasury,
        mock_level,
        mock_get_active,
        mock_refresh,
        mock_on_profit,
        mock_sys_msg,
        mock_announce,
    ):
        """Cop in a police vehicle can auto-arrest nearby suspects."""
        officer, criminal = await self._setup_world()

        await Wanted.objects.acreate(
            character=criminal,
            wanted_remaining=300,
        )

        # Cop is in a police vehicle
        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id,
                    officer.guid,
                    1000,
                    1000,
                    0,
                    vehicle={"name": "Police Car", "unique_id": 100},
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 1500, 1000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        prev = {}
        sc = {}
        with patch(
            "amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        mock_teleport.assert_called_once()

    async def test_no_arrest_in_civilian_vehicle(
        self,
        mock_popup,
        mock_exit_vehicle,
        mock_teleport,
        mock_transfer,
        mock_treasury,
        mock_level,
        mock_get_active,
        mock_refresh,
        mock_on_profit,
        mock_sys_msg,
        mock_announce,
    ):
        """Cop in a civilian vehicle cannot auto-arrest suspects."""
        officer, criminal = await self._setup_world()

        await Wanted.objects.acreate(
            character=criminal,
            wanted_remaining=300,
        )

        # Cop is in a civilian vehicle
        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id,
                    officer.guid,
                    1000,
                    1000,
                    0,
                    vehicle={"name": "Longhorn Semi", "unique_id": 200},
                ),
                _make_player_data(
                    criminal.player.unique_id, criminal.guid, 1500, 1000, 0
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        prev = {}
        sc = {}
        with patch(
            "amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        mock_teleport.assert_not_called()

    async def test_auto_arrest_on_foot_with_null_vehicle_key(
        self,
        mock_popup,
        mock_exit_vehicle,
        mock_teleport,
        mock_transfer,
        mock_treasury,
        mock_level,
        mock_get_active,
        mock_refresh,
        mock_on_profit,
        mock_sys_msg,
        mock_announce,
    ):
        """Regression: cop and suspect on foot with vehicle=None are handled correctly.

        The game server includes ``"vehicle": null`` for on-foot players.
        Previously, ``'vehicle' in pdata`` evaluated to True, causing
        on-foot cops to be wrongly treated as in a civilian vehicle and
        skipped by the police-vehicle gate.
        """
        officer, criminal = await self._setup_world()

        await Wanted.objects.acreate(
            character=criminal,
            wanted_remaining=300,
        )

        # Both on foot — vehicle key is explicitly None (matches real game server)
        players = _make_players_list(
            [
                _make_player_data(
                    officer.player.unique_id,
                    officer.guid,
                    1000,
                    1000,
                    0,
                    vehicle=None,
                ),
                _make_player_data(
                    criminal.player.unique_id,
                    criminal.guid,
                    1500,
                    1000,
                    0,
                    vehicle=None,
                ),
            ]
        )

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        prev = {}
        sc = {}
        with patch(
            "amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players
        ):
            for _ in range(_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        # Arrest should fire — on-foot cop is not blocked by police vehicle gate
        mock_teleport.assert_called_once()


class IsWantedTests(TestCase):
    """Unit tests for _is_wanted helper."""

    async def test_returns_true_with_active_wanted(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await Wanted.objects.acreate(character=character, wanted_remaining=300)
        result = await _is_wanted(character)
        self.assertTrue(result)

    async def test_returns_false_without_wanted(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        result = await _is_wanted(character)
        self.assertFalse(result)

    async def test_returns_false_with_expired_wanted(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await Wanted.objects.acreate(character=character, wanted_remaining=0)
        result = await _is_wanted(character)
        self.assertFalse(result)
