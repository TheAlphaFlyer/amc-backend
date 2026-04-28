"""Tests for the guilds system (amc.guilds)."""

import asyncio
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone

from amc.factories import CharacterFactory, PlayerFactory
from amc.guilds import (
    _activate_guild,
    _end_active_session,
    _find_matching_guild_with_engine,
    handle_guild_session,
)
from amc.models import Guild, GuildCharacter, GuildSession, VehicleDecal


class FindMatchingGuildTests(TestCase):
    """Unit tests for _find_matching_guild_with_engine."""

    async def _create_guild(self, **kwargs):
        defaults = {
            "name": "Test Guild",
            "abbreviation": "TST",
            "vehicle_key": "Trophy2",
        }
        defaults.update(kwargs)
        return await Guild.objects.acreate(**defaults)

    async def test_no_guilds_returns_none(self):
        result = await _find_matching_guild_with_engine("Trophy 2", "guid123", AsyncMock())
        self.assertIsNone(result)

    async def test_vehicle_key_match_no_engine(self):
        guild = await self._create_guild()
        result = await _find_matching_guild_with_engine("Trophy 2", "guid123", AsyncMock())
        self.assertEqual(result, guild)

    async def test_unknown_vehicle_name_returns_none(self):
        await self._create_guild()
        result = await _find_matching_guild_with_engine("UnknownVehicle", "guid123", AsyncMock())
        self.assertIsNone(result)

    async def test_wrong_vehicle_returns_none(self):
        await self._create_guild(vehicle_key="Trophy2")
        result = await _find_matching_guild_with_engine("Hana", "guid123", AsyncMock())
        self.assertIsNone(result)

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_engine_match_success(self, mock_parts):
        guild = await self._create_guild(engine_part_key="Bike_I4_90HP")
        mock_parts.return_value = {
            "parts": [
                {"Key": "SomeBody", "Slot": 1},
                {"Key": "Bike_I4_90HP", "Slot": 2},
            ]
        }

        result = await _find_matching_guild_with_engine("Trophy 2", "guid123", AsyncMock())
        self.assertEqual(result, guild)
        mock_parts.assert_awaited_once()

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_engine_mismatch_returns_none(self, mock_parts):
        await self._create_guild(engine_part_key="Bike_I4_90HP")
        mock_parts.return_value = {
            "parts": [
                {"Key": "DifferentEngine", "Slot": 2},
            ]
        }

        result = await _find_matching_guild_with_engine("Trophy 2", "guid123", AsyncMock())
        self.assertIsNone(result)

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_engine_mismatch_falls_back_to_no_engine_guild(self, mock_parts):
        no_engine_guild = await self._create_guild(
            name="No Engine Guild", abbreviation="NOE", engine_part_key=None
        )
        await self._create_guild(
            name="Engine Guild", abbreviation="ENG", engine_part_key="Bike_I4_90HP"
        )
        mock_parts.return_value = {
            "parts": [
                {"Key": "DifferentEngine", "Slot": 2},
            ]
        }

        result = await _find_matching_guild_with_engine("Trophy 2", "guid123", AsyncMock())
        self.assertEqual(result, no_engine_guild)

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_no_engine_in_parts_falls_back(self, mock_parts):
        no_engine_guild = await self._create_guild(
            name="No Engine Guild", abbreviation="NOE", engine_part_key=None
        )
        await self._create_guild(
            name="Engine Guild", abbreviation="ENG", engine_part_key="Bike_I4_90HP"
        )
        mock_parts.return_value = {
            "parts": [
                {"Key": "SomeBody", "Slot": 1},
            ]
        }

        result = await _find_matching_guild_with_engine("Trophy 2", "guid123", AsyncMock())
        self.assertEqual(result, no_engine_guild)

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_api_failure_returns_none(self, mock_parts):
        await self._create_guild(engine_part_key="Bike_I4_90HP")
        mock_parts.side_effect = Exception("mod server down")

        result = await _find_matching_guild_with_engine("Trophy 2", "guid123", AsyncMock())
        self.assertIsNone(result)

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_no_api_call_when_no_engine_requirement(self, mock_parts):
        await self._create_guild(engine_part_key=None)

        await _find_matching_guild_with_engine("Trophy 2", "guid123", AsyncMock())
        mock_parts.assert_not_called()

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_correct_engine_selected_among_multiple(self, mock_parts):
        await self._create_guild(
            name="Guild A", abbreviation="GA", engine_part_key="Engine_A"
        )
        guild_b = await self._create_guild(
            name="Guild B", abbreviation="GB", engine_part_key="Engine_B"
        )
        mock_parts.return_value = {
            "parts": [
                {"Key": "Engine_B", "Slot": 2},
            ]
        }

        result = await _find_matching_guild_with_engine("Trophy 2", "guid123", AsyncMock())
        self.assertEqual(result, guild_b)


class EndActiveSessionTests(TestCase):
    """Tests for _end_active_session."""

    async def test_ends_active_session(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(
            name="Test", abbreviation="TST", vehicle_key="Trophy2"
        )
        session = await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )
        self.assertIsNone(session.ended_at)

        await _end_active_session(character)

        await session.arefresh_from_db()
        self.assertIsNotNone(session.ended_at)

    async def test_no_active_session_no_error(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        # Should not raise
        await _end_active_session(character)

    async def test_only_ends_active_sessions(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(
            name="Test", abbreviation="TST", vehicle_key="Trophy2"
        )
        past = timezone.now() - timezone.timedelta(hours=1)
        ended_session = await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=past, ended_at=past + timezone.timedelta(minutes=30)
        )
        active_session = await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )

        await _end_active_session(character)

        await ended_session.arefresh_from_db()
        await active_session.arefresh_from_db()
        self.assertIsNotNone(ended_session.ended_at)
        self.assertIsNotNone(active_session.ended_at)


class ActivateGuildTests(TestCase):
    """Tests for _activate_guild."""

    async def test_creates_session_and_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(
            name="Test", abbreviation="TST", vehicle_key="Trophy2"
        )
        mock_session = AsyncMock()

        await _activate_guild(character, guild, mock_session, str(player.unique_id))

        session = await GuildSession.objects.aget(character=character, guild=guild)
        self.assertIsNone(session.ended_at)

        gc = await GuildCharacter.objects.aget(character=character, guild=guild)
        self.assertEqual(gc.level, 1)

    async def test_no_duplicate_when_already_active(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(
            name="Test", abbreviation="TST", vehicle_key="Trophy2"
        )
        await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )
        mock_session = AsyncMock()

        await _activate_guild(character, guild, mock_session, str(player.unique_id))

        count = await GuildSession.objects.filter(
            character=character, guild=guild, ended_at__isnull=True
        ).acount()
        self.assertEqual(count, 1)

    async def test_switches_guild(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild_a = await Guild.objects.acreate(
            name="Guild A", abbreviation="GA", vehicle_key="Trophy2"
        )
        guild_b = await Guild.objects.acreate(
            name="Guild B", abbreviation="GB", vehicle_key="Hana"
        )
        await GuildSession.objects.acreate(
            guild=guild_a, character=character, started_at=timezone.now()
        )
        mock_session = AsyncMock()

        await _activate_guild(character, guild_b, mock_session, str(player.unique_id))

        old_session = await GuildSession.objects.aget(character=character, guild=guild_a)
        self.assertIsNotNone(old_session.ended_at)

        new_session = await GuildSession.objects.aget(
            character=character, guild=guild_b, ended_at__isnull=True
        )
        self.assertIsNotNone(new_session)

    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    async def test_decal_applied(self, mock_set_decal):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        decal_config = {"layers": [{"type": "solid", "color": "FF0000"}]}
        decal = await VehicleDecal.objects.acreate(
            name="Guild Decal",
            hash="testhash123",
            config=decal_config,
        )
        guild = await Guild.objects.acreate(
            name="Test", abbreviation="TST", vehicle_key="Trophy2", decal=decal
        )
        mock_session = AsyncMock()

        await _activate_guild(character, guild, mock_session, str(player.unique_id))

        mock_set_decal.assert_awaited_once_with(
            mock_session, str(player.unique_id), decal_config
        )

    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    async def test_no_decal_when_none(self, mock_set_decal):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(
            name="Test", abbreviation="TST", vehicle_key="Trophy2"
        )
        mock_session = AsyncMock()

        await _activate_guild(character, guild, mock_session, str(player.unique_id))

        mock_set_decal.assert_not_called()

    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    async def test_decal_failure_does_not_crash(self, mock_set_decal):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        decal = await VehicleDecal.objects.acreate(
            name="Guild Decal",
            hash="testhash456",
            config={"layers": []},
        )
        guild = await Guild.objects.acreate(
            name="Test", abbreviation="TST", vehicle_key="Trophy2", decal=decal
        )
        mock_set_decal.side_effect = Exception("server error")
        mock_session = AsyncMock()

        # Should not raise
        await _activate_guild(character, guild, mock_session, str(player.unique_id))

        # Session should still be created
        session = await GuildSession.objects.aget(character=character, guild=guild)
        self.assertIsNone(session.ended_at)

    async def test_guild_character_not_duplicated(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(
            name="Test", abbreviation="TST", vehicle_key="Trophy2"
        )
        await GuildCharacter.objects.acreate(guild=guild, character=character, level=3)
        mock_session = AsyncMock()

        await _activate_guild(character, guild, mock_session, str(player.unique_id))

        count = await GuildCharacter.objects.filter(
            guild=guild, character=character
        ).acount()
        self.assertEqual(count, 1)
        gc = await GuildCharacter.objects.aget(guild=guild, character=character)
        self.assertEqual(gc.level, 3)


class HandleGuildSessionTests(TestCase):
    """Integration tests for handle_guild_session."""

    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_entered_matching_vehicle(self, mock_parts, mock_decal):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(
            name="Trophy Club", abbreviation="TC", vehicle_key="Trophy2"
        )

        await handle_guild_session(
            character, player, AsyncMock(), "ENTERED", "Trophy 2"
        )

        session = await GuildSession.objects.aget(character=character, guild=guild)
        self.assertIsNone(session.ended_at)
        gc = await GuildCharacter.objects.aget(character=character, guild=guild)
        self.assertEqual(gc.level, 1)
        mock_parts.assert_not_called()

    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_entered_non_matching_vehicle(self, mock_parts, mock_decal):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)

        await handle_guild_session(
            character, player, AsyncMock(), "ENTERED", "Hana"
        )

        count = await GuildSession.objects.filter(character=character).acount()
        self.assertEqual(count, 0)

    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_exited_ends_session(self, mock_parts, mock_decal):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(
            name="Test", abbreviation="TST", vehicle_key="Trophy2"
        )
        await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )

        await handle_guild_session(
            character, player, AsyncMock(), "EXITED", "Trophy 2"
        )

        session = await GuildSession.objects.aget(character=character, guild=guild)
        self.assertIsNotNone(session.ended_at)

    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_entered_different_vehicle_ends_session(self, mock_parts, mock_decal):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(
            name="Test", abbreviation="TST", vehicle_key="Trophy2"
        )
        await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )

        await handle_guild_session(
            character, player, AsyncMock(), "ENTERED", "Hana"
        )

        session = await GuildSession.objects.aget(character=character, guild=guild)
        self.assertIsNotNone(session.ended_at)

    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_entered_with_engine_match(self, mock_parts, mock_decal):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        decal = await VehicleDecal.objects.acreate(
            name="Test Decal", hash="hash789", config={"layers": []}
        )
        guild = await Guild.objects.acreate(
            name="Engine Guild",
            abbreviation="ENG",
            vehicle_key="Trophy2",
            engine_part_key="Bike_I4_90HP",
            decal=decal,
        )
        mock_parts.return_value = {
            "parts": [
                {"Key": "Bike_I4_90HP", "Slot": 2},
            ]
        }
        mock_http = AsyncMock()

        await handle_guild_session(
            character, player, mock_http, "ENTERED", "Trophy 2"
        )

        session = await GuildSession.objects.aget(character=character, guild=guild)
        self.assertIsNone(session.ended_at)
        mock_decal.assert_awaited_once()

    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_entered_with_engine_mismatch(self, mock_parts, mock_decal):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await Guild.objects.acreate(
            name="Engine Guild",
            abbreviation="ENG",
            vehicle_key="Trophy2",
            engine_part_key="Bike_I4_90HP",
        )
        mock_parts.return_value = {
            "parts": [
                {"Key": "DifferentEngine", "Slot": 2},
            ]
        }

        await handle_guild_session(
            character, player, AsyncMock(), "ENTERED", "Trophy 2"
        )

        count = await GuildSession.objects.filter(character=character).acount()
        self.assertEqual(count, 0)

    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_switch_guild_on_vehicle_change(self, mock_parts, mock_decal):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild_a = await Guild.objects.acreate(
            name="Trophy Club", abbreviation="TC", vehicle_key="Trophy2"
        )
        guild_b = await Guild.objects.acreate(
            name="Hana Club", abbreviation="HC", vehicle_key="1"
        )

        await handle_guild_session(
            character, player, AsyncMock(), "ENTERED", "Trophy 2"
        )
        session_a = await GuildSession.objects.aget(character=character, guild=guild_a)
        self.assertIsNone(session_a.ended_at)

        await handle_guild_session(
            character, player, AsyncMock(), "ENTERED", "Hana"
        )
        await session_a.arefresh_from_db()
        self.assertIsNotNone(session_a.ended_at)

        session_b = await GuildSession.objects.aget(
            character=character, guild=guild_b, ended_at__isnull=True
        )
        self.assertIsNotNone(session_b)

    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_exception_does_not_propagate(self, mock_parts, mock_decal):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await Guild.objects.acreate(
            name="Test", abbreviation="TST", vehicle_key="Trophy2"
        )
        mock_decal.side_effect = Exception("unexpected")

        # Should not raise even though internals fail
        await handle_guild_session(
            character, player, AsyncMock(), "ENTERED", "Trophy 2"
        )


class CoalesceTests(TestCase):
    """Tests for the _coalesce singleflight helper in mod_server."""

    async def test_concurrent_calls_share_same_request(self):
        from amc.mod_server import _coalesce

        store: dict = {}
        call_count = 0

        async def slow_fetch():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return {"result": "ok"}

        results = await asyncio.gather(
            _coalesce("key1", store, slow_fetch),
            _coalesce("key1", store, slow_fetch),
            _coalesce("key1", store, slow_fetch),
        )

        self.assertEqual(call_count, 1)
        self.assertEqual(results[0], {"result": "ok"})
        self.assertEqual(results[1], {"result": "ok"})
        self.assertEqual(results[2], {"result": "ok"})
        self.assertEqual(len(store), 0)

    async def test_different_keys_are_independent(self):
        from amc.mod_server import _coalesce

        store: dict = {}
        call_count = 0

        async def fetch():
            nonlocal call_count
            call_count += 1
            return call_count

        r1, r2 = await asyncio.gather(
            _coalesce("a", store, fetch),
            _coalesce("b", store, fetch),
        )

        self.assertEqual(call_count, 2)
        self.assertNotEqual(r1, r2)

    async def test_error_propagates_to_all_waiters(self):
        from amc.mod_server import _coalesce

        store: dict = {}

        async def failing_fetch():
            raise ValueError("boom")

        async def run():
            return await _coalesce("key1", store, failing_fetch)

        with self.assertRaises(ValueError):
            await asyncio.gather(run(), run())

        self.assertEqual(len(store), 0)

    async def test_cleanup_after_success(self):
        from amc.mod_server import _coalesce

        store: dict = {}

        async def fetch():
            return "done"

        await _coalesce("key1", store, fetch)
        self.assertNotIn("key1", store)

    async def test_cleanup_after_error(self):
        from amc.mod_server import _coalesce

        store: dict = {}

        async def failing_fetch():
            raise RuntimeError("fail")

        try:
            await _coalesce("key1", store, failing_fetch)
        except RuntimeError:
            pass

        self.assertNotIn("key1", store)
