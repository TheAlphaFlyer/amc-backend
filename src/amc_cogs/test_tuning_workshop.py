from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from django.test import TestCase
from django.utils import timezone

from amc.models import Player, Character, Voucher
from amc_cogs.models import TuningWorkshopSubmission
from amc_cogs.tuning_workshop import TuningWorkshopCog


class VoucherModelTestCase(TestCase):
    async def test_voucher_is_claimed(self):
        player = await Player.objects.acreate(unique_id=1001, discord_user_id=100)
        voucher = await Voucher.objects.acreate(
            code="V-TEST01", player=player, amount=500_000, reason="Test"
        )
        self.assertFalse(voucher.is_claimed)

        char = await Character.objects.acreate(player=player, name="TestChar")
        voucher.claimed_by = char
        voucher.claimed_at = timezone.now()
        await voucher.asave()
        self.assertTrue(voucher.is_claimed)

    async def test_generate_code(self):
        code = Voucher.generate_code(prefix="TW")
        self.assertTrue(code.startswith("TW-"))
        self.assertEqual(len(code), 9)  # "TW-" + 6 chars


class TuningWorkshopOnThreadCreateTestCase(TestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.bot.user = MagicMock()
        self.bot.user.id = 999999
        self.cog = TuningWorkshopCog(self.bot)

    async def test_thread_create_records_submission(self):
        thread = MagicMock()
        thread.parent = MagicMock()
        thread.parent_id = TuningWorkshopCog.FORUM_CHANNEL_ID
        thread.id = 12345
        thread.owner_id = 67890
        thread.send = AsyncMock()
        thread.join = AsyncMock()

        await self.cog.on_thread_create(thread)

        sub = await TuningWorkshopSubmission.objects.aget(thread_id=12345)
        self.assertFalse(sub.skipped)
        self.assertEqual(sub.author_discord_id, 67890)
        self.assertEqual(sub.rewarded_reaction_count, 0)

    async def test_thread_create_sends_welcome_embed(self):
        thread = MagicMock()
        thread.parent = MagicMock()
        thread.parent_id = TuningWorkshopCog.FORUM_CHANNEL_ID
        thread.id = 12345
        thread.owner_id = 67890
        thread.send = AsyncMock()
        thread.join = AsyncMock()

        await self.cog.on_thread_create(thread)

        thread.send.assert_called_once()
        embed = thread.send.call_args.kwargs["embed"]
        self.assertIn("100,000", embed.description)
        self.assertIn("/claim_reward", embed.description)
        self.assertIn("accumulate", embed.description.lower())
        self.assertEqual(embed.color, discord.Color.blue())

    async def test_thread_create_skipped_sends_limit_embed(self):
        now = timezone.now()
        for i in range(2):
            await TuningWorkshopSubmission.objects.acreate(
                thread_id=100 + i,
                author_discord_id=67890,
                created_at=now - timedelta(days=i),
            )

        thread = MagicMock()
        thread.parent = MagicMock()
        thread.parent_id = TuningWorkshopCog.FORUM_CHANNEL_ID
        thread.id = 200
        thread.owner_id = 67890
        thread.send = AsyncMock()
        thread.join = AsyncMock()

        await self.cog.on_thread_create(thread)

        sub = await TuningWorkshopSubmission.objects.aget(thread_id=200)
        self.assertTrue(sub.skipped)

        embed = thread.send.call_args.kwargs["embed"]
        self.assertIn("weekly limit", embed.description.lower())
        self.assertEqual(embed.color, discord.Color.orange())

    async def test_thread_create_ignores_other_channels(self):
        thread = MagicMock()
        thread.parent = MagicMock()
        thread.parent_id = 999

        await self.cog.on_thread_create(thread)
        self.assertEqual(await TuningWorkshopSubmission.objects.acount(), 0)

    async def test_thread_create_no_parent(self):
        thread = MagicMock()
        thread.parent = None

        await self.cog.on_thread_create(thread)
        self.assertEqual(await TuningWorkshopSubmission.objects.acount(), 0)


class ClaimRewardCommandTestCase(TestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.bot.user = MagicMock()
        self.bot.user.id = 999999
        self.cog = TuningWorkshopCog(self.bot)

    def _make_interaction(self, thread_id, user_id, reactions=None):
        interaction = AsyncMock()
        thread = MagicMock(spec=discord.Thread)
        thread.parent_id = TuningWorkshopCog.FORUM_CHANNEL_ID
        thread.id = thread_id
        thread.send = AsyncMock()
        interaction.channel = thread
        interaction.user = MagicMock(id=user_id)
        interaction.response = AsyncMock()

        if reactions is not None:
            starter_message = MagicMock()
            starter_message.reactions = reactions
            thread.fetch_message = AsyncMock(return_value=starter_message)

        return interaction

    async def test_claim_first_time(self):
        """First claim pays out all reactions, voucher tied to author's Player."""
        now = timezone.now()
        player = await Player.objects.acreate(unique_id=5001, discord_user_id=67890)
        await TuningWorkshopSubmission.objects.acreate(
            thread_id=12345, author_discord_id=67890, created_at=now,
        )

        user1 = MagicMock(id=11111, bot=False)
        user2 = MagicMock(id=22222, bot=False)
        reaction = MagicMock()
        reaction.users = MagicMock(return_value=AsyncIterator([user1, user2]))

        interaction = self._make_interaction(12345, 67890, reactions=[reaction])

        await self.cog.claim_reward.callback(self.cog, interaction)

        # Ephemeral response with code (only visible to author)
        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args
        self.assertIn("claim_voucher", call_kwargs[0][0])
        self.assertTrue(call_kwargs[1]["ephemeral"])

        # Public embed posted WITHOUT the code
        interaction.channel.send.assert_called_once()
        embed = interaction.channel.send.call_args.kwargs["embed"]
        self.assertIn("200,000", embed.description)
        self.assertNotIn("TW-", embed.description)  # Code not in public embed

        # DB updated
        sub = await TuningWorkshopSubmission.objects.aget(thread_id=12345)
        self.assertEqual(sub.reaction_count, 2)
        self.assertEqual(sub.rewarded_reaction_count, 2)

        # Voucher created and tied to author's player
        self.assertEqual(await Voucher.objects.acount(), 1)
        voucher = await Voucher.objects.afirst()
        self.assertEqual(voucher.amount, 200_000)
        self.assertEqual(voucher.player_id, player.pk)

    async def test_claim_delta_only(self):
        """Second claim pays out only new reactions since last claim."""
        now = timezone.now()
        await Player.objects.acreate(unique_id=5001, discord_user_id=67890)
        await TuningWorkshopSubmission.objects.acreate(
            thread_id=12345,
            author_discord_id=67890,
            created_at=now,
            reaction_count=2,
            rewarded_reaction_count=2,  # Already claimed 2
        )

        # Now 4 unique reactors
        users = [MagicMock(id=10000 + i, bot=False) for i in range(4)]
        reaction = MagicMock()
        reaction.users = MagicMock(return_value=AsyncIterator(users))

        interaction = self._make_interaction(12345, 67890, reactions=[reaction])

        await self.cog.claim_reward.callback(self.cog, interaction)

        # Should pay for 2 new reactions only
        sub = await TuningWorkshopSubmission.objects.aget(thread_id=12345)
        self.assertEqual(sub.rewarded_reaction_count, 4)

        voucher = await Voucher.objects.afirst()
        self.assertEqual(voucher.amount, 200_000)  # 2 new * 100k
        self.assertIn("2 new", voucher.reason)

    async def test_claim_no_new_reactions(self):
        """Claim with no new reactions since last cashout."""
        now = timezone.now()
        await TuningWorkshopSubmission.objects.acreate(
            thread_id=12345,
            author_discord_id=67890,
            created_at=now,
            reaction_count=2,
            rewarded_reaction_count=2,
        )

        users = [MagicMock(id=10000 + i, bot=False) for i in range(2)]
        reaction = MagicMock()
        reaction.users = MagicMock(return_value=AsyncIterator(users))

        interaction = self._make_interaction(12345, 67890, reactions=[reaction])

        await self.cog.claim_reward.callback(self.cog, interaction)

        self.assertIn("No new reactions", interaction.response.send_message.call_args[0][0])
        self.assertEqual(await Voucher.objects.acount(), 0)

    async def test_claim_no_reactions_at_all(self):
        now = timezone.now()
        await TuningWorkshopSubmission.objects.acreate(
            thread_id=12345, author_discord_id=67890, created_at=now,
        )

        interaction = self._make_interaction(12345, 67890, reactions=[])

        await self.cog.claim_reward.callback(self.cog, interaction)

        self.assertIn("no reactions", interaction.response.send_message.call_args[0][0])

    async def test_claim_wrong_channel(self):
        interaction = AsyncMock()
        interaction.channel = MagicMock(spec=[])  # not a Thread
        interaction.response = AsyncMock()

        await self.cog.claim_reward.callback(self.cog, interaction)

        self.assertIn("only be used inside", interaction.response.send_message.call_args[0][0])

    async def test_claim_not_author(self):
        now = timezone.now()
        await TuningWorkshopSubmission.objects.acreate(
            thread_id=12345, author_discord_id=67890, created_at=now,
        )

        interaction = self._make_interaction(12345, 99999)  # different user

        await self.cog.claim_reward.callback(self.cog, interaction)

        self.assertIn("Only the thread author", interaction.response.send_message.call_args[0][0])

    async def test_claim_skipped(self):
        now = timezone.now()
        await TuningWorkshopSubmission.objects.acreate(
            thread_id=12345, author_discord_id=67890, created_at=now, skipped=True,
        )

        interaction = self._make_interaction(12345, 67890)

        await self.cog.claim_reward.callback(self.cog, interaction)

        self.assertIn("skipped", interaction.response.send_message.call_args[0][0])

    async def test_claim_no_linked_account(self):
        """Claim fails if author has no linked Player record."""
        now = timezone.now()
        await TuningWorkshopSubmission.objects.acreate(
            thread_id=12345, author_discord_id=67890, created_at=now,
        )

        user1 = MagicMock(id=11111, bot=False)
        reaction = MagicMock()
        reaction.users = MagicMock(return_value=AsyncIterator([user1]))

        interaction = self._make_interaction(12345, 67890, reactions=[reaction])

        await self.cog.claim_reward.callback(self.cog, interaction)

        self.assertIn("No linked game account", interaction.response.send_message.call_args[0][0])
        self.assertEqual(await Voucher.objects.acount(), 0)

    async def test_claim_excludes_bot_and_author(self):
        """Reactions from bots and the post author are excluded."""
        now = timezone.now()
        await Player.objects.acreate(unique_id=5001, discord_user_id=67890)
        await TuningWorkshopSubmission.objects.acreate(
            thread_id=12345, author_discord_id=67890, created_at=now,
        )

        user = MagicMock(id=11111, bot=False)
        bot_user = MagicMock(id=999999, bot=True)
        author = MagicMock(id=67890, bot=False)
        reaction = MagicMock()
        reaction.users = MagicMock(return_value=AsyncIterator([user, bot_user, author]))

        interaction = self._make_interaction(12345, 67890, reactions=[reaction])

        await self.cog.claim_reward.callback(self.cog, interaction)

        voucher = await Voucher.objects.afirst()
        self.assertEqual(voucher.amount, 100_000)  # Only 1 valid reactor


class ClaimVoucherCommandTestCase(TestCase):
    async def test_claim_voucher_by_code(self):
        from amc.commands.finance import cmd_claim_voucher

        player = await Player.objects.acreate(unique_id=3001, discord_user_id=300)
        char = await Character.objects.acreate(player=player, name="ClaimChar")

        voucher = await Voucher.objects.acreate(
            code="TW-ABC123", amount=300_000, reason="Tuning Workshop: 3 reactions"
        )

        ctx = MagicMock()
        ctx.player = player
        ctx.character = char
        ctx.reply = AsyncMock()

        with patch("amc.commands.finance.send_fund_to_player", new_callable=AsyncMock) as mock_send:
            await cmd_claim_voucher(ctx, code="TW-ABC123")
            mock_send.assert_called_once_with(300_000, char, "Voucher: Tuning Workshop: 3 reactions")

        await voucher.arefresh_from_db()
        self.assertTrue(voucher.is_claimed)
        self.assertEqual(voucher.player_id, player.pk)

    async def test_claim_voucher_invalid_code(self):
        from amc.commands.finance import cmd_claim_voucher

        player = await Player.objects.acreate(unique_id=3002, discord_user_id=301)
        char = await Character.objects.acreate(player=player, name="Bad")

        ctx = MagicMock()
        ctx.player = player
        ctx.character = char
        ctx.reply = AsyncMock()

        await cmd_claim_voucher(ctx, code="INVALID")
        self.assertIn("Invalid Code", ctx.reply.call_args[0][0])


class AsyncIterator:
    """Helper to make a list behave as an async iterator for reaction.users()."""

    def __init__(self, items):
        self.items = items
        self.index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.items):
            raise StopAsyncIteration
        item = self.items[self.index]
        self.index += 1
        return item
