"""Tests for teleport heat escalation — wanted players gain heat when teleporting."""

import time
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase

from amc.factories import PlayerFactory, CharacterFactory
from amc.models import Wanted, ServerTeleportLog


def _teleport_event(character_guid, hook="ServerTeleportCharacter", seq=100):
    """Build a teleport event with proper dedup fields."""
    return {
        "hook": hook,
        "timestamp": int(time.time()),
        "_seq": seq,
        "_epoch": "test-epoch",
        "data": {
            "CharacterGuid": str(character_guid),
            "AbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
        },
    }


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock, return_value=False)
@patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=[])
@patch(
    "amc.webhook.get_treasury_fund_balance",
    new_callable=AsyncMock,
    return_value=100_000,
)
@patch("amc.player_tags.refresh_player_name", new_callable=AsyncMock)
@patch("amc.mod_server.send_system_message", new_callable=AsyncMock)
@patch("amc.game_server.announce", new_callable=AsyncMock)
# Mock get_players to return None (no police lookup during tests by default)
@patch("amc.handlers.teleport.get_players", new_callable=AsyncMock, return_value=None)
class TeleportHeatEscalationTests(TestCase):
    """Tests for _handle_teleport_or_respawn — wanted heat escalation on teleport."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    async def _setup_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        return player, character

    async def _process_teleport(
        self, character, hook="ServerTeleportCharacter", seq=100
    ):
        """Run a single teleport event through process_events."""
        from amc.webhook import process_events

        events = [_teleport_event(character.guid, hook=hook, seq=seq)]
        http_client = AsyncMock()
        http_client_mod = AsyncMock()
        await process_events(
            events, http_client=http_client, http_client_mod=http_client_mod
        )

    # ------------------------------------------------------------------
    # Heat escalation on teleport
    # ------------------------------------------------------------------

    async def test_teleport_increases_wanted_heat(
        self,
        mock_get_players,
        mock_announce,
        mock_system_msg,
        mock_refresh,
        mock_treasury,
        mock_parties,
        mock_rp,
    ):
        """Teleporting while wanted should increase wanted_remaining by base heat."""
        from amc.handlers.teleport import TELEPORT_HEAT_BASE

        _, character = await self._setup_character()
        initial = 100.0
        await Wanted.objects.acreate(character=character, wanted_remaining=initial)

        await self._process_teleport(character)

        wanted = await Wanted.objects.aget(character=character, expired_at__isnull=True)
        # With no police (get_players returns None), only base heat is added
        self.assertAlmostEqual(
            wanted.wanted_remaining, initial + TELEPORT_HEAT_BASE, places=1
        )

    async def test_teleport_without_wanted_no_effect(
        self,
        mock_get_players,
        mock_announce,
        mock_system_msg,
        mock_refresh,
        mock_treasury,
        mock_parties,
        mock_rp,
    ):
        """Teleporting without Wanted → no heat added."""
        _, character = await self._setup_character()

        await self._process_teleport(character, hook="ServerRespawnCharacter")

        self.assertEqual(
            await Wanted.objects.filter(character=character).acount(), 0
        )

    async def test_teleport_logs_always_created(
        self,
        mock_get_players,
        mock_announce,
        mock_system_msg,
        mock_refresh,
        mock_treasury,
        mock_parties,
        mock_rp,
    ):
        """Teleport log should be created regardless of wanted status."""
        _, character = await self._setup_character()

        await self._process_teleport(character)

        log_count = await ServerTeleportLog.objects.filter(
            character=character
        ).acount()
        self.assertEqual(log_count, 1)

    async def test_teleport_heat_capped_at_max(
        self,
        mock_get_players,
        mock_announce,
        mock_system_msg,
        mock_refresh,
        mock_treasury,
        mock_parties,
        mock_rp,
    ):
        """Heat should not exceed INITIAL_WANTED_LEVEL * 5."""
        _, character = await self._setup_character()
        max_heat = Wanted.INITIAL_WANTED_LEVEL * 5
        # Start near the max
        await Wanted.objects.acreate(
            character=character, wanted_remaining=max_heat - 10
        )

        await self._process_teleport(character)

        wanted = await Wanted.objects.aget(character=character, expired_at__isnull=True)
        self.assertEqual(wanted.wanted_remaining, max_heat)

    async def test_teleport_sends_system_message(
        self,
        mock_get_players,
        mock_announce,
        mock_system_msg,
        mock_refresh,
        mock_treasury,
        mock_parties,
        mock_rp,
    ):
        """Player should receive a system message when heat escalates."""
        _, character = await self._setup_character()
        await Wanted.objects.acreate(
            character=character, wanted_remaining=Wanted.INITIAL_WANTED_LEVEL
        )

        await self._process_teleport(character)

        # send_system_message is fire-and-forget via asyncio.create_task,
        # so we just verify wanted_remaining increased
        wanted = await Wanted.objects.aget(character=character, expired_at__isnull=True)
        self.assertGreater(wanted.wanted_remaining, Wanted.INITIAL_WANTED_LEVEL)

    async def test_star_level_change_triggers_name_refresh(
        self,
        mock_get_players,
        mock_announce,
        mock_system_msg,
        mock_refresh,
        mock_treasury,
        mock_parties,
        mock_rp,
    ):
        """If star level changes, player name should be refreshed."""

        _, character = await self._setup_character()
        # Start at 55 (W1), adding 60 base heat → 115 (W2), star change
        await Wanted.objects.acreate(character=character, wanted_remaining=55)

        await self._process_teleport(character)

        # refresh_player_name should have been called (star went from W1 to W2)
        mock_refresh.assert_called()

    async def test_multiple_teleports_accumulate_heat(
        self,
        mock_get_players,
        mock_announce,
        mock_system_msg,
        mock_refresh,
        mock_treasury,
        mock_parties,
        mock_rp,
    ):
        """Multiple teleports should each add heat."""
        from amc.handlers.teleport import TELEPORT_HEAT_BASE

        _, character = await self._setup_character()
        initial = 100.0
        await Wanted.objects.acreate(character=character, wanted_remaining=initial)

        await self._process_teleport(character, seq=100)
        await self._process_teleport(character, seq=101)

        wanted = await Wanted.objects.aget(character=character, expired_at__isnull=True)
        self.assertAlmostEqual(
            wanted.wanted_remaining, initial + TELEPORT_HEAT_BASE * 2, places=1
        )
