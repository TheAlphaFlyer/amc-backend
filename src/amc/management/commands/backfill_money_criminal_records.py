from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from amc.models import CriminalRecord, ServerCargoArrivedLog


class Command(BaseCommand):
    help = "Backfill criminal records for recent Money deliveries (last 7 days)"

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=7)

        # Backfill cargo_key on rows that only have it in JSON data
        updated = ServerCargoArrivedLog.objects.filter(
            data__Net_CargoKey="Money",
            timestamp__gte=cutoff,
        ).exclude(cargo_key="Money").update(cargo_key="Money")
        if updated:
            self.stdout.write(f"  Updated cargo_key on {updated} rows")

        # Get distinct characters who delivered Money in the last 7 days
        character_ids = (
            ServerCargoArrivedLog.objects.filter(
                Q(cargo_key="Money") | Q(data__Net_CargoKey="Money"),
                timestamp__gte=cutoff,
                character__isnull=False,
            )
            .values_list("character_id", flat=True)
            .distinct()
        )

        created = 0
        extended = 0
        for character_id in character_ids:
            active = CriminalRecord.objects.filter(
                character_id=character_id, expires_at__gt=timezone.now()
            ).first()
            if active:
                active.expires_at = active.expires_at + timedelta(days=7)
                active.save(update_fields=["expires_at"])
                self.stdout.write(
                    f"  Extended record for character {character_id} → expires {active.expires_at}"
                )
                extended += 1
            else:
                # Use the latest delivery timestamp as the record start
                latest_ts = (
                    ServerCargoArrivedLog.objects.filter(
                        Q(cargo_key="Money") | Q(data__Net_CargoKey="Money"),
                        character_id=character_id,
                        timestamp__gte=cutoff,
                    )
                    .order_by("-timestamp")
                    .values_list("timestamp", flat=True)
                    .first()
                )
                if latest_ts:
                    CriminalRecord.objects.create(
                        character_id=character_id,
                        reason="Money delivery",
                        expires_at=latest_ts + timedelta(days=7),
                    )
                    created += 1
                    self.stdout.write(f"  Created record for character {character_id}")

        self.stdout.write(
            self.style.SUCCESS(f"Backfill done. Created {created}, extended {extended}.")
        )
