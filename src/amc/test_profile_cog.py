import discord
from django.test import TestCase
from unittest.mock import AsyncMock, MagicMock
from datetime import timedelta
from django.utils import timezone
from typing import Any, cast
from psycopg.types.range import Range

from amc_cogs.profile import PlayerProfileCog
from amc.models import (
    Player,
    Character,
    PlayerStatusLog,
    ServerCargoArrivedLog,
)
from amc.enums import CargoKey


class PlayerProfileCogTestCase(TestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.bot.http_client_game = MagicMock()
        self.cog = PlayerProfileCog(self.bot)

        self.interaction = AsyncMock(spec=discord.Interaction)
        self.interaction.response = AsyncMock()
        self.interaction.followup = AsyncMock()
        self.interaction.user = MagicMock()
        self.interaction.user.id = 123456789
        self.interaction.user.display_name = "TestUser"

    async def test_player_profile_unverified(self):
        """User with no Player record sees error."""
        await cast(Any, self.cog.player_profile.callback)(self.cog, self.interaction)
        self.interaction.followup.send.assert_called_once()
        args, kwargs = self.interaction.followup.send.call_args
        self.assertIn("verified", args[0])

    async def test_player_profile_basic(self):
        """Verified user sees embed with levels, donations, last online."""
        now = timezone.now()
        player = await Player.objects.acreate(
            unique_id=76561198000000001,
            discord_user_id=123456789,
            discord_name="TestUser",
        )
        char = await Character.objects.acreate(
            player=player,
            name="TestChar",
            driver_level=5,
            bus_level=3,
            taxi_level=2,
            police_level=1,
            truck_level=7,
            wrecker_level=4,
            racer_level=2,
            total_donations=1_234_567,
            last_online=now - timedelta(hours=1),
        )
        await PlayerStatusLog.objects.acreate(
            character=char,
            timespan=Range(now - timedelta(hours=2), now),
        )

        await cast(Any, self.cog.player_profile.callback)(self.cog, self.interaction)

        self.interaction.followup.send.assert_called_once()
        embed = self.interaction.followup.send.call_args.kwargs["embed"]

        self.assertIn("TestChar", embed.title)

        # Check levels field
        levels_field = embed.fields[0]
        self.assertEqual(levels_field.name, "📊 Levels")
        self.assertIn("**Driver:** 5", levels_field.value)
        self.assertIn("**Truck:** 7", levels_field.value)

        # Check economy field
        economy_field = embed.fields[1]
        self.assertIn("$1,234,567", economy_field.value)

        # Check activity field
        activity_field = embed.fields[2]
        self.assertIn("2.0h", activity_field.value)

    async def test_player_profile_with_deliveries(self):
        """Embed includes cargo breakdown."""
        now = timezone.now()
        player = await Player.objects.acreate(
            unique_id=76561198000000002,
            discord_user_id=123456789,
        )
        char = await Character.objects.acreate(player=player, name="DeliveryGuy")
        await PlayerStatusLog.objects.acreate(
            character=char,
            timespan=Range(now - timedelta(hours=1), now),
        )

        # Create deliveries
        await ServerCargoArrivedLog.objects.acreate(
            player=player,
            character=char,
            cargo_key=CargoKey.AppleBox,
            payment=1000,
            weight=100.0,
            timestamp=now - timedelta(days=1),
        )
        await ServerCargoArrivedLog.objects.acreate(
            player=player,
            character=char,
            cargo_key=CargoKey.AppleBox,
            payment=1500,
            weight=150.0,
            timestamp=now - timedelta(days=2),
        )
        await ServerCargoArrivedLog.objects.acreate(
            player=player,
            character=char,
            cargo_key=CargoKey.CarrotBox,
            payment=500,
            weight=50.0,
            timestamp=now - timedelta(days=3),
        )

        await cast(Any, self.cog.player_profile.callback)(self.cog, self.interaction)

        embed = self.interaction.followup.send.call_args.kwargs["embed"]

        # Find the deliveries field
        delivery_field = None
        for field in embed.fields:
            if "Deliveries" in field.name:
                delivery_field = field
                break

        self.assertIsNotNone(delivery_field)
        self.assertIn("3 total", delivery_field.name)
        self.assertIn("$3,000", delivery_field.name)
        self.assertIn("Apples", delivery_field.value)
        self.assertIn("Carrots", delivery_field.value)

    async def test_player_profile_specific_player(self):
        """Looking up another player by character ID."""
        now = timezone.now()
        other_player = await Player.objects.acreate(unique_id=777)
        char = await Character.objects.acreate(
            player=other_player, name="Lucky", driver_level=10
        )
        await PlayerStatusLog.objects.acreate(
            character=char,
            timespan=Range(now - timedelta(hours=1), now),
        )

        await cast(Any, self.cog.player_profile.callback)(
            self.cog, self.interaction, character=str(char.id)
        )

        embed = self.interaction.followup.send.call_args.kwargs["embed"]
        self.assertIn("Lucky", embed.title)
        self.assertIn("**Driver:** 10", embed.fields[0].value)

    async def test_player_profile_not_found(self):
        """Invalid character ID returns error."""
        await cast(Any, self.cog.player_profile.callback)(
            self.cog, self.interaction, character="999999"
        )
        args, kwargs = self.interaction.followup.send.call_args
        self.assertIn("not found", args[0])
