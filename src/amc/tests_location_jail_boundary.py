"""Tests for _check_jail_boundary in locations.py."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

from amc.factories import PlayerFactory, CharacterFactory
from amc.models import TeleportPoint


# ── helpers ──────────────────────────────────────────────────────────────────

JAIL_POINT = Point(1_000, 2_000, 3_000, srid=0)


def _make_ctx(http_client_mod=None):
    """Build a minimal ctx dict for _check_jail_boundary."""
    return {"http_client_mod": http_client_mod or AsyncMock()}


async def _make_jailed_character(offset_seconds=0):
    """Create a player + character with jailed_at set."""
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    character.jailed_at = timezone.now() - timedelta(seconds=offset_seconds)
    return player, character


async def _make_jail_tp():
    """Create (or get) the 'jail' TeleportPoint at JAIL_POINT."""
    tp, _ = await TeleportPoint.objects.aget_or_create(
        name="jail",
        character=None,
        defaults={"location": JAIL_POINT},
    )
    return tp


# ── test class ────────────────────────────────────────────────────────────────


@patch("amc.locations.teleport_player", new_callable=AsyncMock)
class JailBoundaryTests(TestCase):
    """Tests for _check_jail_boundary.

    The class-level @patch replaces ``amc.locations.teleport_player`` for every
    test method.  Each method receives ``mock_tp`` as its first argument.
    """

    async def asyncSetUp(self):
        await _make_jail_tp()

    # ── fast-path: player not jailed ─────────────────────────────────────────

    async def test_not_jailed_returns_immediately(self, mock_tp):
        """When jailed_at is None, no DB hit and no teleport should occur."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        # jailed_at is None (default)
        ctx = _make_ctx()

        from amc.locations import _check_jail_boundary

        await _check_jail_boundary(character, Point(99_999, 99_999, 0, srid=0), ctx)

        mock_tp.assert_not_awaited()
        self.assertIsNone(character.jailed_at)

    # ── auto-release after 30 s ───────────────────────────────────────────────

    async def test_expired_jail_clears_jailed_at(self, mock_tp):
        """After JAIL_DURATION_SECONDS, jailed_at is cleared and no teleport fired."""
        from amc.locations import JAIL_DURATION_SECONDS, _check_jail_boundary

        _, character = await _make_jailed_character(
            offset_seconds=JAIL_DURATION_SECONDS  # exactly at expiry boundary
        )
        ctx = _make_ctx()

        await _check_jail_boundary(character, Point(99_999, 0, 0, srid=0), ctx)

        mock_tp.assert_not_awaited()
        self.assertIsNone(character.jailed_at)

    async def test_well_past_expiry_clears_jailed_at(self, mock_tp):
        """Far past auto-release → jailed_at cleared, no teleport."""
        from amc.locations import _check_jail_boundary

        _, character = await _make_jailed_character(offset_seconds=120)
        ctx = _make_ctx()

        await _check_jail_boundary(character, Point(99_999, 0, 0, srid=0), ctx)

        mock_tp.assert_not_awaited()
        self.assertIsNone(character.jailed_at)

    # ── within bounds ─────────────────────────────────────────────────────────

    async def test_inside_boundary_no_teleport(self, mock_tp):
        """Player within JAIL_BOUNDARY_RADIUS → no teleport."""
        from amc.locations import JAIL_BOUNDARY_RADIUS, _check_jail_boundary

        _, character = await _make_jailed_character()
        # Place player 800 units from jail center (< 1000 threshold)
        near_point = Point(
            JAIL_POINT.x + JAIL_BOUNDARY_RADIUS - 200,
            JAIL_POINT.y,
            JAIL_POINT.z,
            srid=0,
        )
        ctx = _make_ctx()

        await _check_jail_boundary(character, near_point, ctx)

        mock_tp.assert_not_awaited()

    async def test_exactly_at_boundary_no_teleport(self, mock_tp):
        """Player exactly at JAIL_BOUNDARY_RADIUS → no teleport (uses <=)."""
        from amc.locations import JAIL_BOUNDARY_RADIUS, _check_jail_boundary

        _, character = await _make_jailed_character()
        on_boundary = Point(
            JAIL_POINT.x + JAIL_BOUNDARY_RADIUS,
            JAIL_POINT.y,
            JAIL_POINT.z,
            srid=0,
        )
        ctx = _make_ctx()

        await _check_jail_boundary(character, on_boundary, ctx)

        mock_tp.assert_not_awaited()

    # ── out of bounds ─────────────────────────────────────────────────────────

    async def test_outside_boundary_teleports_back(self, mock_tp):
        """Player beyond JAIL_BOUNDARY_RADIUS → teleported to jail."""
        import amc.locations as locations_mod
        from amc.locations import JAIL_BOUNDARY_RADIUS

        # Ensure jail TeleportPoint exists (guard against asyncSetUp race)
        await _make_jail_tp()

        _, character = await _make_jailed_character()
        far_point = Point(
            JAIL_POINT.x + JAIL_BOUNDARY_RADIUS + 500,  # 1,500 units away
            JAIL_POINT.y,
            JAIL_POINT.z,
            srid=0,
        )
        ctx = _make_ctx()

        await locations_mod._check_jail_boundary(character, far_point, ctx)

        mock_tp.assert_awaited_once()
        # Verify destination is jail coords
        call_args = mock_tp.call_args
        dest = call_args[0][2]  # positional arg 3 = location dict
        self.assertEqual(dest["X"], JAIL_POINT.x)
        self.assertEqual(dest["Y"], JAIL_POINT.y)
        self.assertEqual(dest["Z"], JAIL_POINT.z)
        # force=True must be set
        self.assertTrue(call_args[1].get("force"))

    async def test_just_over_boundary_triggers_teleport(self, mock_tp):
        """One unit beyond boundary → triggers teleport (boundary is strict >)."""
        import amc.locations as locations_mod
        from amc.locations import JAIL_BOUNDARY_RADIUS

        # Ensure jail TeleportPoint exists (guard against asyncSetUp race)
        await _make_jail_tp()

        _, character = await _make_jailed_character()
        just_outside = Point(
            JAIL_POINT.x + JAIL_BOUNDARY_RADIUS + 1,
            JAIL_POINT.y,
            JAIL_POINT.z,
            srid=0,
        )
        ctx = _make_ctx()

        await locations_mod._check_jail_boundary(character, just_outside, ctx)

        mock_tp.assert_awaited_once()

    # ── missing jail TeleportPoint ────────────────────────────────────────────

    async def test_no_jail_tp_no_crash(self, mock_tp):
        """If 'jail' TeleportPoint is missing → logs a warning and does not crash."""
        from amc.locations import _check_jail_boundary

        await TeleportPoint.objects.filter(name__iexact="jail").adelete()

        _, character = await _make_jailed_character()
        far_point = Point(99_999, 0, 0, srid=0)
        ctx = _make_ctx()

        with patch("amc.locations.logger") as mock_logger:
            await _check_jail_boundary(character, far_point, ctx)

        mock_tp.assert_not_awaited()
        mock_logger.warning.assert_called_once()

    # ── no http_client_mod in ctx ─────────────────────────────────────────────

    async def test_no_http_client_mod_no_crash(self, mock_tp):
        """If ctx has no http_client_mod → returns silently without teleporting."""
        from amc.locations import _check_jail_boundary

        _, character = await _make_jailed_character()
        far_point = Point(99_999, 0, 0, srid=0)
        ctx = {"http_client_mod": None}

        await _check_jail_boundary(character, far_point, ctx)

        mock_tp.assert_not_awaited()
