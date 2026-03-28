import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from django.utils import timezone
from django.conf import settings

if TYPE_CHECKING:
    from amc.discord_client import AMCDiscordBot

from amc.models import Player, FactionChoice, FactionMembership
from amc.utils import format_timedelta

logger = logging.getLogger(__name__)

FACTION_ROLE_MAP = {
    FactionChoice.COP: "DISCORD_COP_ROLE_ID",
    FactionChoice.CRIMINAL: "DISCORD_CRIMINAL_ROLE_ID",
}

FACTION_CHANNEL_MAP = {
    FactionChoice.COP: "DISCORD_COP_CHANNEL_ID",
    FactionChoice.CRIMINAL: "DISCORD_CRIMINAL_CHANNEL_ID",
}

FACTION_EMOJI = {
    FactionChoice.COP: "🚔",
    FactionChoice.CRIMINAL: "🔫",
}


async def sync_faction_discord_role(guild, member, new_faction, old_faction=None):
    """Add the new faction role and remove the old one (if any)."""
    try:
        # Remove old faction role
        if old_faction:
            old_role_id = getattr(settings, FACTION_ROLE_MAP.get(old_faction, ""), 0)
            if old_role_id:
                old_role = guild.get_role(old_role_id)
                if old_role:
                    await member.remove_roles(old_role, reason="Faction switch")

        # Add new faction role
        new_role_id = getattr(settings, FACTION_ROLE_MAP.get(new_faction, ""), 0)
        if new_role_id:
            new_role = guild.get_role(new_role_id)
            if new_role:
                await member.add_roles(new_role, reason="Faction join")
    except Exception as e:
        logger.exception(f"Failed to sync faction Discord role: {e}")


async def remove_faction_discord_role(guild, member, faction):
    """Remove a faction role from a Discord member."""
    try:
        role_id = getattr(settings, FACTION_ROLE_MAP.get(faction, ""), 0)
        if role_id:
            role = guild.get_role(role_id)
            if role:
                await member.remove_roles(role, reason="Faction leave")
    except Exception as e:
        logger.exception(f"Failed to remove faction Discord role: {e}")


class FactionCog(commands.Cog):
    def __init__(self, bot: "AMCDiscordBot"):
        self.bot = bot

    async def _notify_faction_channel(self, member, faction):
        """Send a join notification to the faction's channel."""
        channel_setting = FACTION_CHANNEL_MAP.get(faction, "")
        channel_id = getattr(settings, channel_setting, 0)
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.abc.Messageable):
            return
        emoji = FACTION_EMOJI.get(faction, "")
        label = FactionChoice(faction).label
        embed = discord.Embed(
            description=f"{emoji} **{member.display_name}** has joined the **{label}** faction!",
            color=discord.Color.blue() if faction == FactionChoice.COP else discord.Color.red(),
        )
        await channel.send(embed=embed)

    @app_commands.command(
        name="faction",
        description="Join, switch, or leave a faction (Cops or Criminals)",
    )
    @app_commands.describe(
        action="Choose an action: join cop, join criminal, leave, or check your status"
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Join Cops", value="cop"),
            app_commands.Choice(name="Join Criminals", value="criminal"),
            app_commands.Choice(name="Leave Faction", value="leave"),
            app_commands.Choice(name="Check Status", value="status"),
        ]
    )
    async def faction(self, interaction: discord.Interaction, action: str):
        await interaction.response.defer(ephemeral=True)

        # Look up verified player
        try:
            player = await Player.objects.aget(discord_user_id=interaction.user.id)
        except Player.DoesNotExist:
            await interaction.followup.send(
                "❌ You are not verified. Please use the `/verify` command first.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        member = interaction.user
        if not guild or not isinstance(member, discord.Member):
            await interaction.followup.send(
                "❌ This command must be used in a server.", ephemeral=True
            )
            return

        if action == "status":
            await self._handle_status(interaction, player)
        elif action == "leave":
            await self._handle_leave(interaction, player, guild, member)
        elif action in ("cop", "criminal"):
            await self._handle_join_or_switch(interaction, player, guild, member, action)
        else:
            await interaction.followup.send(
                "❌ Unknown action. Use `cop`, `criminal`, `leave`, or `status`.",
                ephemeral=True,
            )

    async def _handle_status(self, interaction: discord.Interaction, player):
        try:
            membership = await FactionMembership.objects.aget(player=player)
            faction_label = membership.get_faction_display()
            remaining = membership.cooldown_remaining
            if remaining.total_seconds() > 0:
                cooldown_str = f"⏳ Cooldown remaining: **{format_timedelta(remaining)}**"
            else:
                cooldown_str = "✅ You can switch factions now."

            embed = discord.Embed(
                title=f"Your Faction: {faction_label}",
                description=cooldown_str,
                color=discord.Color.blue(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except FactionMembership.DoesNotExist:
            embed = discord.Embed(
                title="No Faction",
                description=(
                    "You are not in a faction.\n\n"
                    "Use `/faction` and choose **Join Cops** or **Join Criminals** to get started."
                ),
                color=discord.Color.greyple(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    async def _handle_leave(self, interaction, player, guild, member):
        try:
            membership = await FactionMembership.objects.aget(player=player)
            old_faction = membership.faction
            old_label = membership.get_faction_display()
            await membership.adelete()
            await remove_faction_discord_role(guild, member, old_faction)
            await interaction.followup.send(
                f"✅ You have left the **{old_label}** faction. Your role has been removed.",
                ephemeral=True,
            )
        except FactionMembership.DoesNotExist:
            await interaction.followup.send(
                "❌ You are not in a faction.", ephemeral=True
            )

    async def _handle_join_or_switch(self, interaction, player, guild, member, action):
        faction_map = {
            "cop": FactionChoice.COP,
            "criminal": FactionChoice.CRIMINAL,
        }
        new_faction = faction_map[action]
        new_label = FactionChoice(new_faction).label

        try:
            membership = await FactionMembership.objects.aget(player=player)

            # Already in same faction
            if membership.faction == new_faction:
                await interaction.followup.send(
                    f"You are already a **{new_label}**!", ephemeral=True
                )
                return

            # Check cooldown
            remaining = membership.cooldown_remaining
            if remaining.total_seconds() > 0:
                embed = discord.Embed(
                    title="⏳ Cooldown Active",
                    description=(
                        f"You cannot switch factions yet.\n"
                        f"**Time remaining:** {format_timedelta(remaining)}"
                    ),
                    color=discord.Color.orange(),
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            # Switch faction
            old_faction = membership.faction
            membership.faction = new_faction
            membership.last_switched_at = timezone.now()
            await membership.asave(update_fields=["faction", "last_switched_at"])

            await sync_faction_discord_role(guild, member, new_faction, old_faction)
            await self._notify_faction_channel(member, new_faction)

            embed = discord.Embed(
                title=f"Faction Switched to {new_label}!",
                description=(
                    f"You are now a **{new_label}**.\n"
                    f"⚠️ You cannot switch again for **{settings.FACTION_SWITCH_COOLDOWN_HOURS} hours**."
                ),
                color=discord.Color.green(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except FactionMembership.DoesNotExist:
            # First time joining — no cooldown
            await FactionMembership.objects.acreate(
                player=player,
                faction=new_faction,
            )
            await sync_faction_discord_role(guild, member, new_faction)
            await self._notify_faction_channel(member, new_faction)

            embed = discord.Embed(
                title=f"Welcome to the {new_label} faction!",
                description=f"You are now a **{new_label}**. Your Discord role has been updated.",
                color=discord.Color.green(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
