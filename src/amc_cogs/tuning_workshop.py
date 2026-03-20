import logging
from datetime import timedelta

import discord
from discord import app_commands
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
            embed = discord.Embed(
                title="🔧 Tuning Workshop",
                description=(
                    "⚠️ You've reached the weekly limit of "
                    f"**{self.MAX_POSTS_PER_WEEK} rewardable posts**.\n\n"
                    "This post will not be eligible for rewards. "
                    "Try again next week!"
                ),
                color=discord.Color.orange(),
            )
        else:
            reward_at = now + timedelta(days=self.REWARD_DELAY_DAYS)
            embed = discord.Embed(
                title="🔧 Tuning Workshop",
                description=(
                    "Thanks for your submission! Here's how rewards work:\n\n"
                    f"💰 You earn **${self.REWARD_PER_REACTION:,}** for each unique reaction from other members.\n"
                    f"⏰ Rewards are automatically issued **{self.REWARD_DELAY_DAYS} days** after posting "
                    f"(<t:{int(reward_at.timestamp())}:R>).\n"
                    "⚡ Want your reward sooner? Use `/claim_reward` in this thread to claim early — "
                    "but no further reactions will be counted.\n"
                    "🎟️ You'll receive a voucher code to redeem in-game with `/claim_voucher <code>`."
                ),
                color=discord.Color.blue(),
            )
            embed.set_footer(
                text=f"Weekly limit: {self.MAX_POSTS_PER_WEEK} rewardable posts per user"
            )

        await thread.send(embed=embed)

    @app_commands.command(
        name="claim_reward",
        description="Claim your tuning workshop reward early",
    )
    async def claim_reward(self, interaction: discord.Interaction):
        """Let the thread author claim their reward before the 7-day wait."""
        channel = interaction.channel

        # Must be in a thread within the tuning workshop forum
        if (
            not isinstance(channel, discord.Thread)
            or channel.parent_id != self.FORUM_CHANNEL_ID
        ):
            await interaction.response.send_message(
                "❌ This command can only be used inside a #tuning-workshop thread.",
                ephemeral=True,
            )
            return

        # Look up submission
        try:
            submission = await TuningWorkshopSubmission.objects.aget(
                thread_id=channel.id
            )
        except TuningWorkshopSubmission.DoesNotExist:
            await interaction.response.send_message(
                "❌ No tracked submission found for this thread.",
                ephemeral=True,
            )
            return

        # Must be the author
        if interaction.user.id != submission.author_discord_id:
            await interaction.response.send_message(
                "❌ Only the thread author can claim the reward.",
                ephemeral=True,
            )
            return

        # Already processed or skipped
        if submission.processed:
            await interaction.response.send_message(
                "❌ This submission has already been processed.",
                ephemeral=True,
            )
            return
        if submission.skipped:
            await interaction.response.send_message(
                "❌ This submission was skipped (weekly limit exceeded).",
                ephemeral=True,
            )
            return

        # Count current reactions for the preview
        try:
            starter_message = await channel.fetch_message(channel.id)
        except (discord.NotFound, discord.HTTPException):
            await interaction.response.send_message(
                "❌ Could not fetch the original post to count reactions.",
                ephemeral=True,
            )
            return

        unique_reactors = set()
        for reaction in starter_message.reactions:
            async for user in reaction.users():
                if user.bot or user.id == submission.author_discord_id:
                    continue
                unique_reactors.add(user.id)

        reaction_count = len(unique_reactors)
        reward_amount = reaction_count * self.REWARD_PER_REACTION

        if reaction_count == 0:
            await interaction.response.send_message(
                "❌ This post has no reactions from other users yet. "
                "Wait for some reactions before claiming!",
                ephemeral=True,
            )
            return

        # Show confirmation
        view = ClaimRewardConfirmView(
            cog=self, submission=submission, timeout=60
        )
        embed = discord.Embed(
            title="⚡ Claim Reward Early?",
            description=(
                f"Your post currently has **{reaction_count}** unique reaction{'s' if reaction_count != 1 else ''}.\n\n"
                f"💰 Reward: **${reward_amount:,}**\n\n"
                "⚠️ **Warning:** Claiming now means **no further reactions will be counted**. "
                "The reward amount shown above is final.\n\n"
                f"The automatic reward would be issued <t:{int(submission.reward_at.timestamp())}:R>."
            ),
            color=discord.Color.yellow(),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

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


class ClaimRewardConfirmView(discord.ui.View):
    """Confirmation view for early reward claiming."""

    def __init__(self, cog: TuningWorkshopCog, submission: TuningWorkshopSubmission, timeout: float):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.submission = submission

    @discord.ui.button(label="Confirm & Claim", style=discord.ButtonStyle.green, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Re-check that submission hasn't been processed in the meantime
        await self.submission.arefresh_from_db()
        if self.submission.processed:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="❌ Already Processed",
                    description="This submission has already been processed.",
                    color=discord.Color.red(),
                ),
                view=None,
            )
            return

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="⏳ Processing...",
                description="Counting reactions and issuing your voucher.",
                color=discord.Color.blurple(),
            ),
            view=None,
        )

        await self.cog._process_submission(self.submission)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Cancelled",
                description="Your reward will be automatically issued after the waiting period.",
                color=discord.Color.greyple(),
            ),
            view=None,
        )
