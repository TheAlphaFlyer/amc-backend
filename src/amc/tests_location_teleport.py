"""Tests for location-based teleport detection in locations.py."""

from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.test import TestCase

from amc.factories import PlayerFactory, CharacterFactory
from amc.models import PoliceSession, Wanted


def _make_ctx(http_client_mod=None, http_client=None):
    """Build a minimal ctx dict for monitor_locations / _check_teleport_by_location."""
    return {
        "http_client_mod": http_client_mod or AsyncMock(),
        "http_client": http_client or AsyncMock(),
    }


@patch("amc.handlers.teleport._handle_teleport_or_respawn", new_callable=AsyncMock)
class LocationTeleportDetectionTests(TestCase):
    """Tests for _check_teleport_by_location."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    async def _setup_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        return player, character

    async def test_teleport_detected_for_wanted_player(self, mock_handle):
        """Wanted player moves >100m → heat escalation triggered."""
        _, character = await self._setup_character()
        await Wanted.objects.acreate(
            character=character, wanted_remaining=Wanted.INITIAL_WANTED_LEVEL
        )

        from amc.locations import _check_teleport_by_location

        old_loc = Point(0, 0, 0)
        new_loc = Point(50_000, 0, 0)  # 50,000 units apart = well over threshold
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_awaited_once()
        # Verify the character arg
        call_args = mock_handle.call_args
        self.assertEqual(call_args[0][1], character)

    async def test_normal_driving_no_trigger(self, mock_handle):
        """Wanted player moves <100m between ticks → no trigger."""
        _, character = await self._setup_character()
        await Wanted.objects.acreate(
            character=character, wanted_remaining=Wanted.INITIAL_WANTED_LEVEL
        )

        from amc.locations import _check_teleport_by_location

        old_loc = Point(0, 0, 0)
        new_loc = Point(5_000, 0, 0)  # 5,000 units < 10,000 threshold
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_not_awaited()

    async def test_not_wanted_no_trigger(self, mock_handle):
        """Non-wanted player teleporting → no trigger."""
        _, character = await self._setup_character()

        from amc.locations import _check_teleport_by_location

        old_loc = Point(0, 0, 0)
        new_loc = Point(50_000, 0, 0)
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_not_awaited()

    async def test_expired_wanted_no_trigger(self, mock_handle):
        """Player with expired wanted → no trigger."""
        from django.utils import timezone

        _, character = await self._setup_character()
        await Wanted.objects.acreate(
            character=character,
            wanted_remaining=0,
            expired_at=timezone.now(),
        )

        from amc.locations import _check_teleport_by_location

        old_loc = Point(0, 0, 0)
        new_loc = Point(50_000, 0, 0)
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_not_awaited()

    async def test_police_officer_not_triggered(self, mock_handle):
        """Active police officers should not have heat escalated."""
        _, character = await self._setup_character()
        await Wanted.objects.acreate(
            character=character, wanted_remaining=Wanted.INITIAL_WANTED_LEVEL
        )
        await PoliceSession.objects.acreate(character=character)

        from amc.locations import _check_teleport_by_location

        old_loc = Point(0, 0, 0)
        new_loc = Point(50_000, 0, 0)
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_not_awaited()

    @patch("amc.locations.LOCATION_TELEPORT_DETECTION_ENABLED", False)
    async def test_feature_flag_disabled(self, mock_handle):
        """When feature flag is off → no detection even with valid conditions."""
        _, character = await self._setup_character()
        await Wanted.objects.acreate(
            character=character, wanted_remaining=Wanted.INITIAL_WANTED_LEVEL
        )

        from amc.locations import _check_teleport_by_location

        old_loc = Point(0, 0, 0)
        new_loc = Point(50_000, 0, 0)
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_not_awaited()

    async def test_exactly_at_threshold_no_trigger(self, mock_handle):
        """Distance exactly at threshold → no trigger (uses > not >=)."""
        _, character = await self._setup_character()
        await Wanted.objects.acreate(
            character=character, wanted_remaining=Wanted.INITIAL_WANTED_LEVEL
        )

        from amc.locations import (
            _check_teleport_by_location,
            TELEPORT_DISTANCE_THRESHOLD,
        )

        old_loc = Point(0, 0, 0)
        new_loc = Point(TELEPORT_DISTANCE_THRESHOLD, 0, 0)  # exactly at boundary
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_not_awaited()

    async def test_just_over_threshold_triggers(self, mock_handle):
        """Distance just over threshold + wanted → trigger."""
        _, character = await self._setup_character()
        await Wanted.objects.acreate(
            character=character, wanted_remaining=Wanted.INITIAL_WANTED_LEVEL
        )

        from amc.locations import (
            _check_teleport_by_location,
            TELEPORT_DISTANCE_THRESHOLD,
        )

        old_loc = Point(0, 0, 0)
        new_loc = Point(TELEPORT_DISTANCE_THRESHOLD + 1, 0, 0)
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_awaited_once()
