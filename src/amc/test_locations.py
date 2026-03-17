from unittest.mock import AsyncMock, patch
from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point, Polygon
from django.test import TestCase
from amc.models import ShortcutZone
from amc.factories import CharacterFactory
from amc.locations import (
    _check_shortcut_zones,
    SHORTCUT_ZONE_WARNING_MESSAGE,
)


class ShortcutZoneWarningTests(TestCase):
    """Tests for _check_shortcut_zones proximity warnings."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A 200x200 square polygon centered at (1000, 1000)
        cls.zone_polygon = Polygon(
            ((900, 900), (1100, 900), (1100, 1100), (900, 1100), (900, 900)),
            srid=3857,
        )

    async def _create_zone(self, active=True):
        return await ShortcutZone.objects.acreate(
            name="Test Shortcut",
            polygon=self.zone_polygon,
            active=active,
        )

    def _make_ctx(self, mock_session):
        return {"http_client_mod": mock_session}

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_warning_on_approach(self, mock_show_popup):
        """Player moves from outside 10000 units to within 10000 units → popup fires."""
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        old_loc = Point(-10000, 1000, 0, srid=0)  # 10900 units from polygon edge
        new_loc = Point(-9000, 1000, 0, srid=0)  # 9900 units from polygon edge

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        mock_show_popup.assert_called_once_with(
            ctx["http_client_mod"],
            SHORTCUT_ZONE_WARNING_MESSAGE,
            player_id=character.player.unique_id,
        )

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_no_warning_when_far(self, mock_show_popup):
        """Player stays beyond 10000 units → no popup."""
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        old_loc = Point(-15000, 1000, 0, srid=0)  # 15900 units from edge
        new_loc = Point(-12000, 1000, 0, srid=0)  # 12900 units from edge

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        mock_show_popup.assert_not_called()

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_no_warning_when_already_inside(self, mock_show_popup):
        """Player was already within 10000 units → no duplicate warning."""
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        old_loc = Point(-8000, 1000, 0, srid=0)  # 8900 units from edge (already close)
        new_loc = Point(950, 1000, 0, srid=0)  # inside the polygon (distance=0)

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        mock_show_popup.assert_not_called()

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_inactive_zone_ignored(self, mock_show_popup):
        """Inactive zone should not trigger a warning."""
        await self._create_zone(active=False)
        character = await sync_to_async(CharacterFactory)()

        old_loc = Point(700, 1000, 0, srid=0)
        new_loc = Point(850, 1000, 0, srid=0)

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        mock_show_popup.assert_not_called()
