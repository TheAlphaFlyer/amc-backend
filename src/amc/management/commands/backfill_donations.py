from django.core.management.base import BaseCommand
from django.db.models import Sum

from amc.models import Character
from amc_finance.models import LedgerEntry


class Command(BaseCommand):
    help = "Backfill total_donations for all characters from LedgerEntry records"

    def handle(self, *args, **options):
        characters = Character.objects.all()
        updated = 0

        for character in characters.iterator():
            total = (
                (
                    LedgerEntry.objects.filter_character_donations(character).aggregate(
                        total=Sum("credit")
                    )["total"]
                )
                or 0
            )

            if total > 0:
                character.total_donations = total
                character.save(update_fields=["total_donations"])
                self.stdout.write(f"  {character.name}: {total:,}")
                updated += 1

        self.stdout.write(self.style.SUCCESS(f"Backfilled {updated} characters."))
