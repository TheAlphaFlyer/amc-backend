import asyncio
import time
import logging

from django.core.management.base import BaseCommand

from amc.models import Character
from amc.characterlocation_stats import refresh_vehicle_stats, CharacterLocationStats

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Backfill CharacterLocationStats for all characters with location data. "
        "Processes one character at a time with configurable sleep for throttling."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--sleep",
            type=float,
            default=0.1,
            help="Seconds to sleep between characters (default: 0.1)",
        )
        parser.add_argument(
            "--character-id",
            type=int,
            default=None,
            help="Process only a specific character ID",
        )

    def handle(self, *args, **options):
        asyncio.run(self._async_handle(**options))

    async def _async_handle(self, **options):
        sleep_seconds = options["sleep"]
        character_id = options["character_id"]

        if character_id:
            characters = Character.objects.filter(id=character_id)
        else:
            # All characters that have at least one location row
            from django.db.models import Exists, OuterRef
            from amc.models import CharacterLocation

            characters = Character.objects.filter(
                Exists(
                    CharacterLocation.objects.filter(character=OuterRef("pk"))
                    .values("pk")[:1]
                )
            ).order_by("id")

        total = await characters.acount()
        self.stdout.write(f"Processing {total} characters...")

        processed = 0
        skipped = 0
        start_time = time.time()

        async for character in characters:
            # Check if already computed (skip if up-to-date)
            try:
                existing = await CharacterLocationStats.objects.aget(
                    character=character
                )
                if existing.last_computed_at and existing.total_location_records > 0:
                    # Already has stats — do incremental
                    await refresh_vehicle_stats(character, since=existing.last_computed_at)
                else:
                    await refresh_vehicle_stats(character)
            except CharacterLocationStats.DoesNotExist:
                await refresh_vehicle_stats(character)

            processed += 1
            elapsed = time.time() - start_time
            rate = processed / elapsed if elapsed > 0 else 0

            if processed % 50 == 0 or processed == total:
                self.stdout.write(
                    f"  {processed}/{total} "
                    f"({rate:.1f} chars/s, "
                    f"elapsed: {elapsed:.0f}s)"
                )

            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)

        elapsed = time.time() - start_time
        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone: {processed} characters processed, "
                f"{skipped} skipped in {elapsed:.0f}s."
            )
        )
