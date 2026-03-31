"""Tests for the auto-arrest patrol loop (amc.auto_arrest)."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from amc.auto_arrest import AUTO_ARREST_STILL_TICKS, _has_recent_money_deliveries, _patrol_tick
from amc.factories import CharacterFactory, PlayerFactory
from amc.models import Confiscation, Delivery, PoliceSession, TeleportPoint

from django.contrib.gis.geos import Point


def _make_player_data(unique_id, character_guid, x, y, z, vehicle=None):
    """Build a fake player dict matching the game server /player/list format."""
    data = {
        "unique_id": str(unique_id),
        "character_guid": character_guid,
        "location": f"X={x} Y={y} Z={z}",
    }
    if vehicle:
        data["vehicle"] = vehicle
    return data


def _make_players_list(player_datas):
    """Wrap player datas into the format returned by get_players()."""
    return [(d["unique_id"], d) for d in player_datas]


@patch("amc.auto_arrest.announce", new_callable=AsyncMock)
@patch("amc.auto_arrest.send_system_message", new_callable=AsyncMock)
@patch("amc.commands.faction.send_fund_to_player_wallet", new_callable=AsyncMock)
@patch("amc.commands.faction.record_confiscation_for_level", new_callable=AsyncMock)
@patch("amc.commands.faction.record_treasury_confiscation_income", new_callable=AsyncMock)
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

    async def test_auto_arrests_suspect_with_money_delivery(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """Patrol tick arrests a suspect near police after enough still ticks."""
        officer, criminal = await self._setup_world()

        # Criminal has a recent Money delivery
        await Delivery.objects.acreate(
            character=criminal,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )

        # Both players near each other (within 50m = 5000 game units)
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(criminal.player.unique_id, criminal.guid, 1500, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        # Must accumulate enough still ticks before arrest triggers
        prev_locations = {}
        still_counters = {}
        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(AUTO_ARREST_STILL_TICKS):
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

    async def test_no_arrest_without_money_delivery(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """Patrol tick does NOT arrest suspects without recent Money deliveries."""
        officer, criminal = await self._setup_world()

        # Criminal has NO Money deliveries
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(criminal.player.unique_id, criminal.guid, 1500, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            prev = {}
            sc = {}
            for _ in range(AUTO_ARREST_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        mock_teleport.assert_not_called()
        self.assertEqual(await Confiscation.objects.acount(), 0)

    async def test_no_arrest_when_out_of_range(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """Patrol tick does NOT arrest suspects who are too far from police."""
        officer, criminal = await self._setup_world()

        await Delivery.objects.acreate(
            character=criminal,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )

        # Criminal is 100m away (10000 game units > 1500 auto-arrest radius on foot)
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(criminal.player.unique_id, criminal.guid, 11000, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            prev = {}
            sc = {}
            for _ in range(AUTO_ARREST_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        mock_teleport.assert_not_called()

    async def test_police_not_arrested(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """Police officers are never auto-arrested, even if nearby each other."""
        officer, _ = await self._setup_world()

        # Create a second officer nearby
        officer2_player = await sync_to_async(PlayerFactory)()
        officer2 = await sync_to_async(CharacterFactory)(player=officer2_player)
        officer2.last_online = timezone.now()
        await officer2.asave(update_fields=["last_online"])
        await PoliceSession.objects.acreate(character=officer2)

        # Even if officer2 has money deliveries (shouldn't happen, but edge case)
        await Delivery.objects.acreate(
            character=officer2,
            cargo_key="Money",
            quantity=1,
            payment=50_000,
            timestamp=timezone.now() - timedelta(minutes=1),
        )

        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(officer2.player.unique_id, officer2.guid, 1500, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            prev = {}
            sc = {}
            for _ in range(AUTO_ARREST_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        mock_teleport.assert_not_called()

    async def test_speed_check_blocks_fast_suspects(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """Suspects moving too fast are NOT auto-arrested (speed > 30 km/h)."""
        officer, criminal = await self._setup_world()

        await Delivery.objects.acreate(
            character=criminal,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )

        from amc.commands.faction import _build_player_locations

        # Simulate continuous fast movement: 600 units per tick at 0.5s = 1200 u/s > 556 u/s limit
        # Build prev_locations for the first tick
        prev_criminal_data = _make_player_data(
            criminal.player.unique_id, criminal.guid, 1500, 1000, 0, vehicle="truck"
        )
        prev_locations = _build_player_locations([(prev_criminal_data["unique_id"], prev_criminal_data)])

        # Each call to get_players returns suspect shifted 600 units further
        tick_counter = [0]
        def make_players_for_tick(*args, **kwargs):
            tick_counter[0] += 1
            x = 1500 + 600 * tick_counter[0]
            return _make_players_list([
                _make_player_data(officer.player.unique_id, officer.guid, x, 1000, 0),
                _make_player_data(criminal.player.unique_id, criminal.guid, x, 1000, 0, vehicle="truck"),
            ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        sc = {}
        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, side_effect=make_players_for_tick):
            for _ in range(AUTO_ARREST_STILL_TICKS + 2):
                prev_locations, sc = await _patrol_tick(
                    mock_http, mock_http_mod, prev_locations, sc
                )

        mock_teleport.assert_not_called()

    async def test_old_money_delivery_not_arrested(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """Money deliveries older than the confiscation window don't trigger arrest."""
        officer, criminal = await self._setup_world()

        # Delivery is 15 minutes old (beyond 10 minute window)
        await Delivery.objects.acreate(
            character=criminal,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=15),
        )

        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(criminal.player.unique_id, criminal.guid, 1500, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            prev = {}
            sc = {}
            for _ in range(AUTO_ARREST_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        mock_teleport.assert_not_called()

    async def test_server_announcement_on_arrest(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """Server announcement fires on auto-arrest after enough still ticks."""
        officer, criminal = await self._setup_world()

        await Delivery.objects.acreate(
            character=criminal,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )

        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(criminal.player.unique_id, criminal.guid, 1500, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        prev = {}
        sc = {}
        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(AUTO_ARREST_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        # announce() is called via asyncio.create_task, check it was called
        mock_announce.assert_called_once()
        call_args = mock_announce.call_args[0]
        self.assertIn(officer.name, call_args[0])
        self.assertIn("confiscated", call_args[0])

    async def test_system_message_to_officer(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """System message is sent to the officer on auto-arrest."""
        officer, criminal = await self._setup_world()

        await Delivery.objects.acreate(
            character=criminal,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )

        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(criminal.player.unique_id, criminal.guid, 1500, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        prev = {}
        sc = {}
        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(AUTO_ARREST_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        # System message to officer about auto-arrest
        mock_sys_msg.assert_called()
        # Find the call specifically to the officer
        officer_calls = [
            c for c in mock_sys_msg.call_args_list
            if c.kwargs.get("character_guid") == officer.guid
        ]
        self.assertTrue(len(officer_calls) >= 1)
        self.assertIn("auto-arrested", officer_calls[0][0][1])

    async def test_cooldown_prevents_rearrest(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """After auto-arrest, suspect is NOT arrested again on subsequent ticks."""
        officer, criminal = await self._setup_world()

        await Delivery.objects.acreate(
            character=criminal,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )

        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(criminal.player.unique_id, criminal.guid, 1500, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        # Accumulate still ticks until arrest fires
        prev = {}
        sc = {}
        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(AUTO_ARREST_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        self.assertEqual(mock_teleport.call_count, 1)

        # Additional ticks: should NOT arrest again (delivery already confiscated)
        mock_teleport.reset_mock()
        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(AUTO_ARREST_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        mock_teleport.assert_not_called()

    async def test_single_tick_does_not_arrest(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """A single tick is NOT enough to trigger auto-arrest."""
        officer, criminal = await self._setup_world()

        await Delivery.objects.acreate(
            character=criminal,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )

        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(criminal.player.unique_id, criminal.guid, 1500, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            await _patrol_tick(mock_http, mock_http_mod, {})

        # Should NOT have arrested on a single tick
        mock_teleport.assert_not_called()

    async def test_arrest_still_works_after_drifting_beyond_radius(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """Suspect starting within radius but drifting beyond it slowly is still arrested.

        Radius is only checked on first contact. Subsequent ticks only enforce speed.
        """
        officer, criminal = await self._setup_world()

        await Delivery.objects.acreate(
            character=criminal,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )


        # Tick 1: suspect starts within radius (500 units apart, well within 1500)
        # Ticks 2-5: suspect drifts slowly beyond radius (200 units per tick = 400 u/s < 556 limit)
        tick_counter = [0]
        def make_drifting_players(*args, **kwargs):
            tick_counter[0] += 1
            # Suspect starts at 500 units from officer, drifts 200u per tick
            sus_x = 1000 + 500 + 200 * (tick_counter[0] - 1)
            return _make_players_list([
                _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
                _make_player_data(criminal.player.unique_id, criminal.guid, sus_x, 1000, 0),
            ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        prev = {}
        sc = {}
        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, side_effect=make_drifting_players):
            for _ in range(AUTO_ARREST_STILL_TICKS):
                prev, sc = await _patrol_tick(mock_http, mock_http_mod, prev, sc)

        # By tick 5, suspect is at 1500+800=2300 units away — beyond the 1500 radius
        # But arrest should still fire because radius was only checked on tick 1
        mock_teleport.assert_called_once()


class HasRecentMoneyDeliveriesTests(TestCase):
    """Unit tests for _has_recent_money_deliveries helper."""

    async def test_returns_true_with_recent_delivery(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await Delivery.objects.acreate(
            character=character,
            cargo_key="Money",
            quantity=1,
            payment=50_000,
            timestamp=timezone.now() - timedelta(minutes=5),
        )
        result = await _has_recent_money_deliveries(character)
        self.assertTrue(result)

    async def test_returns_false_without_delivery(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        result = await _has_recent_money_deliveries(character)
        self.assertFalse(result)

    async def test_returns_false_with_old_delivery(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await Delivery.objects.acreate(
            character=character,
            cargo_key="Money",
            quantity=1,
            payment=50_000,
            timestamp=timezone.now() - timedelta(minutes=15),
        )
        result = await _has_recent_money_deliveries(character)
        self.assertFalse(result)

    async def test_returns_false_with_non_money_delivery(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await Delivery.objects.acreate(
            character=character,
            cargo_key="C::Stone",
            quantity=1,
            payment=50_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )
        result = await _has_recent_money_deliveries(character)
        self.assertFalse(result)

    async def test_returns_false_when_delivery_already_confiscated(self):
        """Deliveries linked to an existing Confiscation are excluded."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        delivery = await Delivery.objects.acreate(
            character=character,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )
        # Link delivery to a confiscation
        conf = await Confiscation.objects.acreate(
            character=character,
            officer=None,
            cargo_key="Money",
            amount=80_000,
        )
        await conf.deliveries.aset([delivery.id])

        result = await _has_recent_money_deliveries(character)
        self.assertFalse(result)

