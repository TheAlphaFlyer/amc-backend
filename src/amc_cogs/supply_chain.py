import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from amc.models import SupplyChainEvent, SupplyChainEventTemplate, SupplyChainContribution

logger = logging.getLogger(__name__)


class SupplyChainCog(commands.Cog):
    """Discord cog for supply chain event progress and commands."""

    def __init__(self, bot):
        self.bot = bot
        self.update_loop.start()

    def cog_unload(self):
        self.update_loop.cancel()

    @tasks.loop(seconds=60)
    async def update_loop(self):
        """Periodically update the supply chain event embed."""
        await self.bot.wait_until_ready()
        active_events = SupplyChainEvent.objects.filter_active().prefetch_related(
            "objectives__cargos",
        )
        async for event in active_events:
            try:
                await self._update_event_embed(event)
            except Exception:
                logger.exception("Error updating supply chain embed")

    async def _update_event_embed(self, event: SupplyChainEvent):
        """Create or update the live progress embed for an event."""
        embed = await self._build_event_embed(event)

        channel = self.bot.get_channel(self._get_channel_id())
        if not channel:
            return

        if event.discord_message_id:
            try:
                message = await channel.fetch_message(event.discord_message_id)
                await message.edit(embed=embed)
            except discord.NotFound:
                event.discord_message_id = None

        if not event.discord_message_id:
            message = await channel.send(embed=embed)
            event.discord_message_id = message.id
            await event.asave(update_fields=["discord_message_id"])

    async def _build_event_embed(self, event: SupplyChainEvent) -> discord.Embed:
        """Build the progress embed for a supply chain event."""
        import math

        now = timezone.now()
        remaining = event.end_at - now
        hours_remaining = max(0, remaining.total_seconds() / 3600)

        embed = discord.Embed(
            title=f"📦 Supply Chain: {event.name}",
            description=event.description or "Community supply chain event!",
            color=discord.Color.gold(),
        )

        total_weight = await event.objectives.aaggregate(total=Sum("reward_weight"))
        total_w = total_weight["total"] or 1

        async for obj in event.objectives.prefetch_related("cargos").all():
            cargo_names = ", ".join([c.label async for c in obj.cargos.all()[:3]])
            label = cargo_names or "Any Cargo"

            progress = obj.quantity_fulfilled
            if obj.ceiling:
                pct = min(100, int(progress / obj.ceiling * 100))
                bar = _progress_bar(pct)
                status = f"{bar} {progress}/{obj.ceiling}"
            else:
                status = f"📊 {progress} delivered"

            reward_pct = int(obj.reward_weight / total_w * 100)
            primary_tag = " ⭐" if obj.is_primary else ""

            # Count unique contributors
            contributors = await SupplyChainContribution.objects.filter(
                objective=obj
            ).values("character_id").distinct().acount()

            embed.add_field(
                name=f"{label}{primary_tag} ({reward_pct}% pool)",
                value=f"{status}\n👥 {contributors} contributors",
                inline=False,
            )

        # Calculate current pool from primary objective
        primary = None
        async for obj in event.objectives.all():
            if obj.is_primary:
                primary = obj
                break

        if primary:
            fulfilled = primary.quantity_fulfilled
            if primary.ceiling is not None:
                fulfilled = min(fulfilled, primary.ceiling)
            current_pool = event.reward_per_item * fulfilled
            max_pool = event.reward_per_item * (primary.ceiling or fulfilled)
            pool_text = f"Pool: ${current_pool:,} / ${max_pool:,}"
        else:
            pool_text = f"${event.reward_per_item:,}/item"

        embed.set_footer(
            text=f"⏱ {math.ceil(hours_remaining)}h remaining | {pool_text}"
        )
        return embed

    def _get_channel_id(self):
        return getattr(settings, "DISCORD_JOBS_CHANNEL_ID", 0)

    # ── Commands ─────────────────────────────────────────────────────

    @discord.app_commands.command(
        name="event_status", description="Show active supply chain event progress"
    )
    async def event_status(self, interaction: discord.Interaction):
        event = await SupplyChainEvent.objects.filter_active().afirst()
        if not event:
            await interaction.response.send_message(
                "No active supply chain events right now.", ephemeral=True
            )
            return

        embed = await self._build_event_embed(event)
        await interaction.response.send_message(embed=embed)

    async def template_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        templates = SupplyChainEventTemplate.objects.filter(
            enabled=True, name__icontains=current
        ).order_by("name")[:25]
        return [
            app_commands.Choice(name=t.name[:100], value=t.pk)
            async for t in templates
        ]

    @app_commands.command(
        name="post_event",
        description="Create a supply chain event from a template",
    )
    @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
    @app_commands.describe(
        template="Event template to use",
        duration_hours="Override duration in hours (optional)",
    )
    @app_commands.autocomplete(template=template_autocomplete)
    async def post_event(
        self,
        interaction: discord.Interaction,
        template: int,
        duration_hours: float | None = None,
    ):
        from amc.supply_chain import create_event_from_template

        await interaction.response.defer(ephemeral=True)

        try:
            tmpl = await SupplyChainEventTemplate.objects.aget(pk=template)
        except SupplyChainEventTemplate.DoesNotExist:
            await interaction.followup.send(
                "❌ Template not found.", ephemeral=True
            )
            return

        event = await create_event_from_template(tmpl, duration_hours)

        # Create Discord scheduled event
        guild = interaction.guild
        if guild:
            try:
                scheduled_event = await guild.create_scheduled_event(
                    name=f"📦 {event.name}",
                    description=event.description or "Community supply chain event!",
                    start_time=event.start_at + timedelta(seconds=10),
                    end_time=event.end_at,
                    entity_type=discord.EntityType.external,
                    location="Motor Town",
                )
                logger.info(
                    f"Created Discord scheduled event {scheduled_event.id} "
                    f"for supply chain event {event.name}"
                )
            except Exception:
                logger.exception("Failed to create Discord scheduled event")

        # Post live progress embed
        try:
            await self._update_event_embed(event)
        except Exception:
            logger.exception("Failed to post event embed")

        actual_duration = duration_hours or tmpl.duration_hours
        obj_count = await event.objectives.acount()

        await interaction.followup.send(
            f"✅ Created **{event.name}** (Event #{event.id})\n"
            f"Reward: {event.reward_per_item:,}/item · "
            f"Duration: {actual_duration:.0f}h · "
            f"Objectives: {obj_count}",
            ephemeral=True,
        )


def _progress_bar(percent: int, length: int = 10) -> str:
    filled = int(length * percent / 100)
    return "█" * filled + "░" * (length - filled)
