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

    async def test_voucher_str(self):
        voucher = await Voucher.objects.acreate(
            code="V-TEST02", amount=100_000, reason="Test"
        )
        result = str(voucher)
        self.assertIn("unclaimed", result)
        self.assertIn("100,000", result)
        self.assertIn("V-TEST02", result)

    async def test_generate_code(self):
        code = Voucher.generate_code(prefix="TW")
        self.assertTrue(code.startswith("TW-"))
        self.assertEqual(len(code), 9)  # "TW-" + 6 chars

    async def test_voucher_nullable_player(self):
        voucher = await Voucher.objects.acreate(
            code="V-NOPL01", amount=50_000, reason="No player"
        )
        self.assertIsNone(voucher.player_id)
        self.assertFalse(voucher.is_claimed)


class TuningWorkshopOnThreadCreateTestCase(TestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.bot.user = MagicMock()
        self.bot.user.id = 999999
        self.cog = TuningWorkshopCog(self.bot)

    async def test_thread_create_records_submission(self):
        """New thread in the workshop forum creates a TuningWorkshopSubmission."""
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
        self.assertFalse(sub.processed)
        self.assertEqual(sub.author_discord_id, 67890)
        self.assertAlmostEqual(
            (sub.reward_at - sub.created_at).total_seconds(),
            timedelta(days=7).total_seconds(),
            delta=5,
        )

    async def test_thread_create_sends_welcome_embed(self):
        """Welcome embed is sent explaining reward mechanics."""
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
        self.assertIn("7 days", embed.description)
        self.assertEqual(embed.color, discord.Color.blue())

    async def test_thread_create_skipped_sends_limit_embed(self):
        """When weekly limit is exceeded, a warning embed is sent."""
        now = timezone.now()
        for i in range(2):
            await TuningWorkshopSubmission.objects.acreate(
                thread_id=100 + i,
                author_discord_id=67890,
                created_at=now - timedelta(days=i),
                reward_at=now + timedelta(days=7 - i),
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

        thread.send.assert_called_once()
        embed = thread.send.call_args.kwargs["embed"]
        self.assertIn("weekly limit", embed.description.lower())
        self.assertEqual(embed.color, discord.Color.orange())

    async def test_thread_create_ignores_other_channels(self):
        thread = MagicMock()
        thread.parent = MagicMock()
        thread.parent_id = 999

        await self.cog.on_thread_create(thread)
        count = await TuningWorkshopSubmission.objects.acount()
        self.assertEqual(count, 0)

    async def test_thread_create_no_parent(self):
        thread = MagicMock()
        thread.parent = None

        await self.cog.on_thread_create(thread)
        count = await TuningWorkshopSubmission.objects.acount()
        self.assertEqual(count, 0)

    async def test_weekly_limit_enforced(self):
        now = timezone.now()
        for i in range(2):
            await TuningWorkshopSubmission.objects.acreate(
                thread_id=100 + i,
                author_discord_id=67890,
                created_at=now - timedelta(days=i),
                reward_at=now + timedelta(days=7 - i),
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

    async def test_weekly_limit_different_author_ok(self):
        now = timezone.now()
        for i in range(2):
            await TuningWorkshopSubmission.objects.acreate(
                thread_id=100 + i,
                author_discord_id=11111,
                created_at=now - timedelta(days=i),
                reward_at=now + timedelta(days=7 - i),
            )

        thread = MagicMock()
        thread.parent = MagicMock()
        thread.parent_id = TuningWorkshopCog.FORUM_CHANNEL_ID
        thread.id = 200
        thread.owner_id = 22222
        thread.send = AsyncMock()
        thread.join = AsyncMock()

        await self.cog.on_thread_create(thread)
        sub = await TuningWorkshopSubmission.objects.aget(thread_id=200)
        self.assertFalse(sub.skipped)


class TuningWorkshopRewardProcessingTestCase(TestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.bot.user = MagicMock()
        self.bot.user.id = 999999
        self.cog = TuningWorkshopCog(self.bot)

    async def test_process_rewards_creates_voucher_with_code(self):
        """A due submission with reactions creates a voucher with a code (no player)."""
        now = timezone.now()

        sub = await TuningWorkshopSubmission.objects.acreate(
            thread_id=12345,
            author_discord_id=67890,
            created_at=now - timedelta(days=8),
            reward_at=now - timedelta(days=1),
        )

        mock_thread = AsyncMock()
        mock_thread.id = 12345
        mock_thread.send = AsyncMock()

        user1 = MagicMock(id=11111, bot=False)
        user2 = MagicMock(id=22222, bot=False)
        bot_user = MagicMock(id=999999, bot=True)
        author_user = MagicMock(id=67890, bot=False)

        reaction1 = MagicMock()
        reaction1.users = MagicMock(return_value=AsyncIterator([user1, user2, bot_user]))
        reaction2 = MagicMock()
        reaction2.users = MagicMock(return_value=AsyncIterator([user1, author_user]))

        starter_message = MagicMock()
        starter_message.reactions = [reaction1, reaction2]

        mock_thread.fetch_message = AsyncMock(return_value=starter_message)
        self.bot.fetch_channel = AsyncMock(return_value=mock_thread)

        await self.cog._process_submission(sub)

        await sub.arefresh_from_db()
        self.assertTrue(sub.processed)
        self.assertEqual(sub.reaction_count, 2)

        voucher = await Voucher.objects.aget(pk=sub.voucher_id)
        self.assertEqual(voucher.amount, 200_000)
        self.assertIsNone(voucher.player_id)  # No player assigned
        self.assertTrue(voucher.code.startswith("TW-"))
        self.assertIn("2 reactions", voucher.reason)

        # Verify embed includes the code
        mock_thread.send.assert_called_once()
        embed = mock_thread.send.call_args.kwargs["embed"]
        self.assertIn(voucher.code, embed.description)

    async def test_process_no_reactions(self):
        now = timezone.now()

        sub = await TuningWorkshopSubmission.objects.acreate(
            thread_id=12346,
            author_discord_id=67890,
            created_at=now - timedelta(days=8),
            reward_at=now - timedelta(days=1),
        )

        mock_thread = AsyncMock()
        mock_thread.id = 12346
        mock_thread.send = AsyncMock()

        starter_message = MagicMock()
        starter_message.reactions = []

        mock_thread.fetch_message = AsyncMock(return_value=starter_message)
        self.bot.fetch_channel = AsyncMock(return_value=mock_thread)

        await self.cog._process_submission(sub)

        await sub.arefresh_from_db()
        self.assertTrue(sub.processed)
        self.assertEqual(sub.reaction_count, 0)
        self.assertEqual(await Voucher.objects.acount(), 0)


class ClaimRewardCommandTestCase(TestCase):
    """Tests for the /claim_reward Discord slash command."""

    def setUp(self):
        self.bot = MagicMock()
        self.bot.user = MagicMock()
        self.bot.user.id = 999999
        self.cog = TuningWorkshopCog(self.bot)

    async def test_claim_reward_wrong_channel(self):
        interaction = AsyncMock()
        interaction.channel = MagicMock(spec=[])  # not a Thread
        interaction.response = AsyncMock()

        await self.cog.claim_reward.callback(self.cog, interaction)

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args
        self.assertIn("only be used inside", call_kwargs[0][0])
        self.assertTrue(call_kwargs[1]["ephemeral"])

    async def test_claim_reward_wrong_forum(self):
        interaction = AsyncMock()
        thread = MagicMock(spec=discord.Thread)
        thread.parent_id = 99999  # wrong forum
        interaction.channel = thread
        interaction.response = AsyncMock()

        await self.cog.claim_reward.callback(self.cog, interaction)

        interaction.response.send_message.assert_called_once()
        self.assertIn("only be used inside", interaction.response.send_message.call_args[0][0])

    async def test_claim_reward_no_submission(self):
        interaction = AsyncMock()
        thread = MagicMock(spec=discord.Thread)
        thread.parent_id = TuningWorkshopCog.FORUM_CHANNEL_ID
        thread.id = 99999
        interaction.channel = thread
        interaction.response = AsyncMock()

        await self.cog.claim_reward.callback(self.cog, interaction)

        self.assertIn(
            "No tracked submission",
            interaction.response.send_message.call_args[0][0],
        )

    async def test_claim_reward_not_author(self):
        now = timezone.now()
        await TuningWorkshopSubmission.objects.acreate(
            thread_id=12345,
            author_discord_id=67890,
            created_at=now,
            reward_at=now + timedelta(days=7),
        )

        interaction = AsyncMock()
        thread = MagicMock(spec=discord.Thread)
        thread.parent_id = TuningWorkshopCog.FORUM_CHANNEL_ID
        thread.id = 12345
        interaction.channel = thread
        interaction.user = MagicMock(id=99999)  # different user
        interaction.response = AsyncMock()

        await self.cog.claim_reward.callback(self.cog, interaction)

        self.assertIn(
            "Only the thread author",
            interaction.response.send_message.call_args[0][0],
        )

    async def test_claim_reward_already_processed(self):
        now = timezone.now()
        await TuningWorkshopSubmission.objects.acreate(
            thread_id=12345,
            author_discord_id=67890,
            created_at=now,
            reward_at=now + timedelta(days=7),
            processed=True,
        )

        interaction = AsyncMock()
        thread = MagicMock(spec=discord.Thread)
        thread.parent_id = TuningWorkshopCog.FORUM_CHANNEL_ID
        thread.id = 12345
        interaction.channel = thread
        interaction.user = MagicMock(id=67890)
        interaction.response = AsyncMock()

        await self.cog.claim_reward.callback(self.cog, interaction)

        self.assertIn(
            "already been processed",
            interaction.response.send_message.call_args[0][0],
        )

    async def test_claim_reward_skipped(self):
        now = timezone.now()
        await TuningWorkshopSubmission.objects.acreate(
            thread_id=12345,
            author_discord_id=67890,
            created_at=now,
            reward_at=now + timedelta(days=7),
            skipped=True,
        )

        interaction = AsyncMock()
        thread = MagicMock(spec=discord.Thread)
        thread.parent_id = TuningWorkshopCog.FORUM_CHANNEL_ID
        thread.id = 12345
        interaction.channel = thread
        interaction.user = MagicMock(id=67890)
        interaction.response = AsyncMock()

        await self.cog.claim_reward.callback(self.cog, interaction)

        self.assertIn(
            "skipped",
            interaction.response.send_message.call_args[0][0],
        )

    async def test_claim_reward_no_reactions(self):
        now = timezone.now()
        await TuningWorkshopSubmission.objects.acreate(
            thread_id=12345,
            author_discord_id=67890,
            created_at=now,
            reward_at=now + timedelta(days=7),
        )

        interaction = AsyncMock()
        thread = MagicMock(spec=discord.Thread)
        thread.parent_id = TuningWorkshopCog.FORUM_CHANNEL_ID
        thread.id = 12345
        interaction.channel = thread
        interaction.user = MagicMock(id=67890)
        interaction.response = AsyncMock()

        starter_message = MagicMock()
        starter_message.reactions = []
        thread.fetch_message = AsyncMock(return_value=starter_message)

        await self.cog.claim_reward.callback(self.cog, interaction)

        self.assertIn(
            "no reactions",
            interaction.response.send_message.call_args[0][0],
        )

    async def test_claim_reward_shows_confirmation(self):
        """Valid early claim shows confirmation with reaction count and warning."""
        now = timezone.now()
        await TuningWorkshopSubmission.objects.acreate(
            thread_id=12345,
            author_discord_id=67890,
            created_at=now,
            reward_at=now + timedelta(days=7),
        )

        interaction = AsyncMock()
        thread = MagicMock(spec=discord.Thread)
        thread.parent_id = TuningWorkshopCog.FORUM_CHANNEL_ID
        thread.id = 12345
        interaction.channel = thread
        interaction.user = MagicMock(id=67890)
        interaction.response = AsyncMock()

        user1 = MagicMock(id=11111, bot=False)
        user2 = MagicMock(id=22222, bot=False)

        reaction1 = MagicMock()
        reaction1.users = MagicMock(return_value=AsyncIterator([user1, user2]))

        starter_message = MagicMock()
        starter_message.reactions = [reaction1]
        thread.fetch_message = AsyncMock(return_value=starter_message)

        await self.cog.claim_reward.callback(self.cog, interaction)

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args[1]
        self.assertTrue(call_kwargs["ephemeral"])
        embed = call_kwargs["embed"]
        self.assertIn("2", embed.description)
        self.assertIn("200,000", embed.description)
        self.assertIn("Warning", embed.description)
        self.assertIsNotNone(call_kwargs.get("view"))


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
        self.assertEqual(voucher.claimed_by_id, char.pk)
        self.assertEqual(voucher.player_id, player.pk)  # Player was assigned on claim

        ctx.reply.assert_called_once()
        reply_text = ctx.reply.call_args[0][0]
        self.assertIn("300,000", reply_text)
        self.assertIn("ClaimChar", reply_text)

    async def test_claim_voucher_invalid_code(self):
        from amc.commands.finance import cmd_claim_voucher

        player = await Player.objects.acreate(unique_id=3002, discord_user_id=301)
        char = await Character.objects.acreate(player=player, name="Bad")

        ctx = MagicMock()
        ctx.player = player
        ctx.character = char
        ctx.reply = AsyncMock()

        await cmd_claim_voucher(ctx, code="INVALID")

        reply_text = ctx.reply.call_args[0][0]
        self.assertIn("Invalid Code", reply_text)

    async def test_claim_voucher_already_claimed(self):
        from amc.commands.finance import cmd_claim_voucher

        player = await Player.objects.acreate(unique_id=3003, discord_user_id=302)
        char = await Character.objects.acreate(player=player, name="Already")

        await Voucher.objects.acreate(
            code="TW-USED01",
            player=player,
            amount=100_000,
            reason="Test",
            claimed_by=char,
            claimed_at=timezone.now(),
        )

        ctx = MagicMock()
        ctx.player = player
        ctx.character = char
        ctx.reply = AsyncMock()

        await cmd_claim_voucher(ctx, code="TW-USED01")

        reply_text = ctx.reply.call_args[0][0]
        self.assertIn("Already Claimed", reply_text)

    async def test_claim_voucher_wrong_player(self):
        """Player-linked voucher can't be claimed by a different player."""
        from amc.commands.finance import cmd_claim_voucher

        owner = await Player.objects.acreate(unique_id=3004, discord_user_id=303)
        thief = await Player.objects.acreate(unique_id=3005, discord_user_id=304)
        char = await Character.objects.acreate(player=thief, name="Thief")

        await Voucher.objects.acreate(
            code="TW-OWNED1", player=owner, amount=100_000, reason="Test"
        )

        ctx = MagicMock()
        ctx.player = thief
        ctx.character = char
        ctx.reply = AsyncMock()

        await cmd_claim_voucher(ctx, code="TW-OWNED1")

        reply_text = ctx.reply.call_args[0][0]
        self.assertIn("Not Your Voucher", reply_text)

    async def test_case_insensitive_code(self):
        """Lowercase code input should still match."""
        from amc.commands.finance import cmd_claim_voucher

        player = await Player.objects.acreate(unique_id=3006, discord_user_id=305)
        char = await Character.objects.acreate(player=player, name="CaseInsensitive")

        await Voucher.objects.acreate(
            code="TW-LOWER1", amount=50_000, reason="Test"
        )

        ctx = MagicMock()
        ctx.player = player
        ctx.character = char
        ctx.reply = AsyncMock()

        with patch("amc.commands.finance.send_fund_to_player", new_callable=AsyncMock):
            await cmd_claim_voucher(ctx, code="tw-lower1")

        reply_text = ctx.reply.call_args[0][0]
        self.assertIn("Voucher Claimed", reply_text)


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
