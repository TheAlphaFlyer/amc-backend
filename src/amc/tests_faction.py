from django.test import TestCase
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import timedelta
from django.utils import timezone
from django.conf import settings
from amc_cogs.faction import sync_faction_discord_role, remove_faction_discord_role
from amc.models import Player, FactionChoice, FactionMembership


class FactionMembershipModelTestCase(TestCase):
    def setUp(self):
        self.player = Player.objects.create(unique_id="76561198000000002")

    async def test_cooldown_remaining_none(self):
        """No cooldown when last_switched_at is None."""
        membership = await FactionMembership.objects.acreate(
            player=self.player, faction=FactionChoice.COP
        )
        self.assertEqual(membership.cooldown_remaining, timedelta(0))
        self.assertTrue(membership.can_switch)

    async def test_cooldown_remaining_active(self):
        """Active cooldown returns positive remaining time."""
        membership = await FactionMembership.objects.acreate(
            player=self.player,
            faction=FactionChoice.COP,
            last_switched_at=timezone.now(),
        )
        self.assertTrue(membership.cooldown_remaining > timedelta(0))
        self.assertFalse(membership.can_switch)

    async def test_cooldown_remaining_elapsed(self):
        """Cooldown returns zero after period has elapsed."""
        past = timezone.now() - timedelta(
            hours=settings.FACTION_SWITCH_COOLDOWN_HOURS + 1
        )
        membership = await FactionMembership.objects.acreate(
            player=self.player,
            faction=FactionChoice.COP,
            last_switched_at=past,
        )
        self.assertEqual(membership.cooldown_remaining, timedelta(0))
        self.assertTrue(membership.can_switch)

    async def test_one_to_one_enforcement(self):
        """Only one faction membership per player."""
        from django.db import IntegrityError

        await FactionMembership.objects.acreate(
            player=self.player, faction=FactionChoice.COP
        )
        with self.assertRaises(IntegrityError):
            await FactionMembership.objects.acreate(
                player=self.player, faction=FactionChoice.CRIMINAL
            )


class FactionDiscordRoleSyncTestCase(TestCase):
    def setUp(self):
        self.player = Player.objects.create(
            unique_id="76561198000000003", discord_user_id=99999
        )

    async def test_sync_adds_new_role(self):
        """Sync adds the new faction role."""
        mock_guild = MagicMock()
        mock_member = MagicMock()
        mock_role = MagicMock()

        mock_guild.get_role.return_value = mock_role
        mock_member.add_roles = AsyncMock()
        mock_member.remove_roles = AsyncMock()

        with patch.object(settings, "DISCORD_COP_ROLE_ID", 111):
            await sync_faction_discord_role(
                mock_guild, mock_member, FactionChoice.COP
            )

        mock_member.add_roles.assert_called_once()

    async def test_sync_removes_old_role_and_adds_new(self):
        """Sync removes old role and adds new one when switching."""
        mock_guild = MagicMock()
        mock_member = MagicMock()
        mock_cop_role = MagicMock()
        mock_criminal_role = MagicMock()

        mock_member.add_roles = AsyncMock()
        mock_member.remove_roles = AsyncMock()

        def get_role_side_effect(role_id):
            if role_id == 111:
                return mock_cop_role
            if role_id == 222:
                return mock_criminal_role
            return None

        mock_guild.get_role.side_effect = get_role_side_effect

        with (
            patch.object(settings, "DISCORD_COP_ROLE_ID", 111),
            patch.object(settings, "DISCORD_CRIMINAL_ROLE_ID", 222),
        ):
            await sync_faction_discord_role(
                mock_guild,
                mock_member,
                FactionChoice.CRIMINAL,
                old_faction=FactionChoice.COP,
            )

        mock_member.remove_roles.assert_called_once_with(
            mock_cop_role, reason="Faction switch"
        )
        mock_member.add_roles.assert_called_once_with(
            mock_criminal_role, reason="Faction join"
        )

    async def test_remove_faction_role(self):
        """Remove faction role works correctly."""
        mock_guild = MagicMock()
        mock_member = MagicMock()
        mock_role = MagicMock()

        mock_guild.get_role.return_value = mock_role
        mock_member.remove_roles = AsyncMock()

        with patch.object(settings, "DISCORD_COP_ROLE_ID", 111):
            await remove_faction_discord_role(
                mock_guild, mock_member, FactionChoice.COP
            )

        mock_member.remove_roles.assert_called_once_with(
            mock_role, reason="Faction leave"
        )
