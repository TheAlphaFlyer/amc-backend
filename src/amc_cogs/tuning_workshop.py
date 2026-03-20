import logging
from datetime import timedelta

import discord
from discord.ext import tasks, commands
from django.utils import timezone

from amc.models import Voucher
from amc_cogs.models import TuningWorkshopSubmission

logger = logging.getLogger(__name__)


class TuningWorkshopCog(commands.Cog):
    """Monitors #tuning-workshop forum and rewards posts based on reactions after 7 days."""

    FORUM_CHANNEL_ID = 1353368480988008448
    REWARD_PER_REACTION = 100_000
    REWARD_DELAY_DAYS = 7
    MAX_POSTS_PER_WEEK = 2

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.process_rewards_task.start()

    async def cog_unload(self):
        self.process_rewards_task.cancel()

    @commands.Cog.listener()
    async def on_thread_create(self, thread):
        # Only handle threads in the tuning workshop forum
        if not thread.parent or thread.parent_id != self.FORUM_CHANNEL_ID:
            return

        now = timezone.now()
        author_id = thread.owner_id

        # Check weekly post limit
        week_ago = now - timedelta(days=7)
        recent_count = await TuningWorkshopSubmission.objects.filter(
            author_discord_id=author_id,
            created_at__gte=week_ago,
            skipped=False,
        ).acount()

        skipped = recent_count >= self.MAX_POSTS_PER_WEEK

        await TuningWorkshopSubmission.objects.acreate(
            thread_id=thread.id,
            author_discord_id=author_id,
            created_at=now,
            reward_at=now + timedelta(days=self.REWARD_DELAY_DAYS),
            skipped=skipped,
        )

        if skipped:
            logger.info(
                f"Tuning workshop submission {thread.id} by {author_id} skipped "
                f"(weekly limit of {self.MAX_POSTS_PER_WEEK} reached)"
            )

    @tasks.loop(hours=1)
    async def process_rewards_task(self):
        """Process submissions that have passed their reward_at deadline."""
        now = timezone.now()
        pending = TuningWorkshopSubmission.objects.filter(
            processed=False,
            skipped=False,
            reward_at__lte=now,
        )

        async for submission in pending:
            try:
                await self._process_submission(submission)
            except Exception:
                logger.exception(
                    f"Failed to process tuning workshop submission {submission.thread_id}"
                )

    async def _process_submission(self, submission):
        """Count reactions and issue voucher for a single submission."""
        # Fetch the thread
        try:
            thread = await self.bot.fetch_channel(submission.thread_id)
        except discord.NotFound:
            logger.warning(f"Thread {submission.thread_id} not found, marking processed")
            submission.processed = True
            await submission.asave(update_fields=["processed"])
            return
        except discord.HTTPException:
            logger.warning(f"Failed to fetch thread {submission.thread_id}")
            return

        # Fetch the starter message (same ID as thread)
        try:
            starter_message = await thread.fetch_message(thread.id)
        except (discord.NotFound, discord.HTTPException):
            logger.warning(
                f"Starter message for thread {submission.thread_id} not found"
            )
            submission.processed = True
            await submission.asave(update_fields=["processed"])
            return

        # Count unique reaction users, excluding bot and author
        unique_reactors = set()
        for reaction in starter_message.reactions:
            async for user in reaction.users():
                if user.bot:
                    continue
                if user.id == submission.author_discord_id:
                    continue
                unique_reactors.add(user.id)

        reaction_count = len(unique_reactors)
        submission.reaction_count = reaction_count

        if reaction_count == 0:
            submission.processed = True
            await submission.asave(update_fields=["processed", "reaction_count"])
            await thread.send(
                embed=discord.Embed(
                    title="🔧 Tuning Workshop Results",
                    description="This post received no reactions from other users. No reward issued.",
                    color=discord.Color.greyple(),
                )
            )
            return

        # Create voucher with code (no player — anyone with the code can claim)
        reward_amount = reaction_count * self.REWARD_PER_REACTION
        code = Voucher.generate_code(prefix="TW")
        voucher = await Voucher.objects.acreate(
            code=code,
            amount=reward_amount,
            reason=f"Tuning Workshop: {reaction_count} reactions",
        )

        # Update submission
        submission.voucher = voucher
        submission.processed = True
        await submission.asave(update_fields=["processed", "reaction_count", "voucher"])

        # Post result embed with the voucher code
        embed = discord.Embed(
            title="🔧 Tuning Workshop Reward",
            description=(
                f"This post received **{reaction_count}** unique reaction{'s' if reaction_count != 1 else ''}!\n\n"
                f"💰 A voucher for **${reward_amount:,}** has been issued.\n"
                f"Use `/claim_voucher {code}` in-game to deposit it to your bank account."
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Code: {code}")
        await thread.send(embed=embed)

    @process_rewards_task.before_loop
    async def before_process_rewards(self):
        await self.bot.wait_until_ready()
