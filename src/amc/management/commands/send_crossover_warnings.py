import asyncio
import os

import discord
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from datetime import timedelta

from amc_finance.services import get_crossover_accounts


def _load_discord_token():
    """Get DISCORD_TOKEN from settings, falling back to agenix secrets."""
    token = settings.DISCORD_TOKEN
    if token:
        return token
    secrets_path = "/run/agenix/backend"
    if os.path.exists(secrets_path):
        with open(secrets_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_TOKEN="):
                    return line.split("=", 1)[1].strip("\"'")
    return None


class Command(BaseCommand):
    help = "Send crossover warning DMs to players whose wealth tax exceeds interest"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List crossover accounts without sending DMs",
        )

    def handle(self, *args, **options):
        accounts = get_crossover_accounts()
        self.stdout.write(f"Found {len(accounts)} accounts past the crossover point.\n")

        if options["dry_run"]:
            for a in accounts:
                warned = a.character.crossover_warning_sent_at
                status = f"warned {warned:%Y-%m-%d}" if warned else "not warned"
                self.stdout.write(
                    f"  {a.character.name} (ID:{a.character.id}) "
                    f"${int(a.balance):,} | tax={a.hourly_tax:,}/hr "
                    f"interest={a.hourly_interest:,}/hr | {status}"
                )
            return

        asyncio.run(self._send_warnings(accounts))

    async def _send_warnings(self, accounts):
        token = _load_discord_token()
        if not token:
            raise CommandError(
                "DISCORD_TOKEN not found. Set it in your environment or ensure "
                "/run/agenix/backend is readable."
            )

        intents = discord.Intents.default()
        client = discord.Client(intents=intents)
        ready = asyncio.Event()

        @client.event
        async def on_ready():
            ready.set()

        asyncio.create_task(client.start(token))
        await ready.wait()

        warned = 0
        skipped = 0
        for account in accounts:
            character = account.character
            if character.crossover_warning_sent_at is not None:
                if timezone.now() < character.crossover_warning_sent_at + timedelta(
                    days=30
                ):
                    skipped += 1
                    continue

            player = character.player
            if not player or not player.discord_user_id:
                continue

            try:
                user = await client.fetch_user(player.discord_user_id)
                await user.send(
                    f"📊 **Financial Advisory from the Bank of ASEAN**\n\n"
                    f"Your bank account for **{character.name}** has reached a point "
                    f"where your hourly **wealth tax exceeds your interest earnings**.\n\n"
                    f"**Current Balance:** ${account.balance:,.0f}\n"
                    f"**Hourly Interest:** +${account.hourly_interest:,}\n"
                    f"**Hourly Wealth Tax:** -${account.hourly_tax:,}\n"
                    f"**Net Hourly Change:** -${account.net_hourly_loss:,}\n\n"
                    f"Your balance is now decreasing every hour you remain offline.\n"
                    f"Log back in — even briefly — to reset your tax clock "
                    f"and resume earning full interest."
                )
                warned += 1
                self.stdout.write(
                    self.style.SUCCESS(f"  ✓ {character.name} (ID:{character.id})")
                )
            except discord.Forbidden:
                self.stdout.write(
                    self.style.WARNING(
                        f"  ⊘ {character.name} (ID:{character.id}): DMs disabled"
                    )
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f"  ✗ {character.name} (ID:{character.id}): {e}"
                    )
                )
                continue

            character.crossover_warning_sent_at = timezone.now()
            await character.asave(update_fields=["crossover_warning_sent_at"])

        await client.close()
        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone: {warned} warned, {skipped} skipped (cooldown)."
            )
        )
