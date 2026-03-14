import asyncio

from django.core.management.base import BaseCommand

from amc_finance.services import apply_interest_to_bank_accounts


class Command(BaseCommand):
    help = "Manually trigger bank interest payments for all eligible accounts"

    def handle(self, *args, **options):
        self.stdout.write("Applying bank interest...")
        asyncio.run(apply_interest_to_bank_accounts({}))
        self.stdout.write(self.style.SUCCESS("Done."))
