import asyncio

import discord
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta

from amc_finance.services import get_crossover_accounts


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
        intents = discord.Intents.default()
        client = discord.Client(intents=intents)
        ready = asyncio.Event()

        @client.event
        async def on_ready():
            ready.set()

        asyncio.create_task(client.start(settings.DISCORD_TOKEN))
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
