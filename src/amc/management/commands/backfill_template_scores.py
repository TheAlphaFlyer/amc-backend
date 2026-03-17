import asyncio
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from amc.models import DeliveryJob, DeliveryJobTemplate

logger = logging.getLogger(__name__)

# Same constants as in jobs.py
BOOST_FACTOR = 1.15
DECAY_FACTOR = 0.70
MIN_SCORE = 0.1
MAX_SCORE = 2.0


class Command(BaseCommand):
    help = (
        "Backfill DeliveryJobTemplate success_score and lifetime counters "
        "from historical DeliveryJob records by replaying the EMA."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change without saving",
        )

    def handle(self, *args, **options):
        asyncio.run(self._async_handle(**options))

    async def _async_handle(self, **options):
        dry_run = options["dry_run"]
        now = timezone.now()

        templates = DeliveryJobTemplate.objects.filter(enabled=True)
        total = await templates.acount()
        self.stdout.write(f"Processing {total} templates...")

        updated = 0
        async for template in templates:
            # Get all historical jobs from this template, ordered chronologically
            jobs = DeliveryJob.objects.filter(
                created_from=template,
            ).order_by("requested_at")

            completions = 0
            expirations = 0
            score = 1.0

            async for job in jobs:
                if job.fulfilled_at is not None:
                    # Completed
                    completions += 1
                    score = min(MAX_SCORE, score * BOOST_FACTOR)
                elif job.expired_at and job.expired_at < now and job.fulfilled_at is None:
                    # Expired
                    expirations += 1
                    score = max(MIN_SCORE, score * DECAY_FACTOR)
                # else: still active or not yet expired — skip

            score = round(score, 4)

            if dry_run:
                self.stdout.write(
                    f"  {template.name}: score={score:.4f} "
                    f"(completions={completions}, expirations={expirations})"
                )
            else:
                template.success_score = score
                template.lifetime_completions = completions
                template.lifetime_expirations = expirations
                await template.asave(
                    update_fields=[
                        "success_score",
                        "lifetime_completions",
                        "lifetime_expirations",
                    ]
                )
                updated += 1

            if updated % 20 == 0 and updated > 0:
                self.stdout.write(f"  {updated}/{total} templates updated")

        action = "would update" if dry_run else "updated"
        self.stdout.write(
            self.style.SUCCESS(f"\nDone: {action} {updated or total} templates.")
        )
