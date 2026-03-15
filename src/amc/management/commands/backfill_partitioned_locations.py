import time

from django.core.management.base import BaseCommand
from django.db import connection


# Month boundaries matching the partition setup in migration 0154
MONTHS = [
    ("2025-07-01", "2025-08-01"),
    ("2025-08-01", "2025-09-01"),
    ("2025-09-01", "2025-10-01"),
    ("2025-10-01", "2025-11-01"),
    ("2025-11-01", "2025-12-01"),
    ("2025-12-01", "2026-01-01"),
    ("2026-01-01", "2026-02-01"),
    ("2026-02-01", "2026-03-01"),
    ("2026-03-01", "2026-04-01"),
    ("2026-04-01", "2026-05-01"),
    ("2026-05-01", "2026-06-01"),
    ("2026-06-01", "2026-07-01"),
]


class Command(BaseCommand):
    help = (
        "Backfill historical CharacterLocation rows into the new partitioned table. "
        "Safe to stop and restart — uses ON CONFLICT DO NOTHING for idempotency."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=10_000,
            help="Number of rows to copy per batch (default: 10000)",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=1.0,
            help="Seconds to sleep between batches (default: 1.0)",
        )
        parser.add_argument(
            "--month",
            type=str,
            default=None,
            help="Process only a specific month, e.g. '2025-07' (default: all months)",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        sleep_seconds = options["sleep"]
        target_month = options["month"]

        # Verify the new table exists
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'amc_characterlocation_new'"
            )
            if not cursor.fetchone():
                self.stderr.write(
                    self.style.ERROR(
                        "Table amc_characterlocation_new does not exist. "
                        "Run migration 0154 first."
                    )
                )
                return

        months_to_process = MONTHS
        if target_month:
            months_to_process = [
                (start, end) for start, end in MONTHS if start.startswith(target_month)
            ]
            if not months_to_process:
                self.stderr.write(
                    self.style.ERROR(f"No partition found for month: {target_month}")
                )
                return

        total_copied = 0
        start_time = time.time()

        for month_start, month_end in months_to_process:
            self.stdout.write(
                self.style.NOTICE(f"\n--- Processing {month_start} to {month_end} ---")
            )

            # Get the ID range for this month from old table
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT MIN(id), MAX(id), COUNT(*) FROM amc_characterlocation "
                    'WHERE "timestamp" >= %s AND "timestamp" < %s',
                    [month_start, month_end],
                )
                min_id, max_id, month_count = cursor.fetchone()

            if min_id is None:
                self.stdout.write("  No rows found for this month, skipping.")
                continue

            # Resume: find the max ID already backfilled for this month
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT MAX(id) FROM amc_characterlocation_new "
                    'WHERE "timestamp" >= %s AND "timestamp" < %s',
                    [month_start, month_end],
                )
                resume_id = cursor.fetchone()[0]

            if resume_id is not None and resume_id >= max_id:
                self.stdout.write(
                    f"  Already complete (max backfilled id {resume_id:,} >= {max_id:,}), skipping."
                )
                continue

            start_id = (resume_id + 1) if resume_id is not None else min_id
            if resume_id is not None:
                self.stdout.write(
                    f"  Resuming from id {start_id:,} (already backfilled up to {resume_id:,})"
                )

            self.stdout.write(
                f"  ID range: {start_id:,} — {max_id:,}, total month rows: {month_count:,}"
            )

            month_copied = 0
            current_id = start_id

            while current_id <= max_id:
                batch_end = current_id + batch_size

                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO amc_characterlocation_new "
                        '(id, "timestamp", character_id, location, vehicle_key) '
                        'SELECT id, "timestamp", character_id, location, vehicle_key '
                        "FROM amc_characterlocation "
                        "WHERE id >= %s AND id < %s "
                        'AND "timestamp" >= %s AND "timestamp" < %s '
                        'ON CONFLICT ("timestamp", character_id) DO NOTHING',
                        [current_id, batch_end, month_start, month_end],
                    )
                    rows_inserted = cursor.rowcount

                month_copied += rows_inserted
                total_copied += rows_inserted
                current_id = batch_end

                # Progress
                elapsed = time.time() - start_time
                rate = total_copied / elapsed if elapsed > 0 else 0
                self.stdout.write(
                    f"  Batch {current_id:,} / {max_id:,} — "
                    f"inserted {rows_inserted:,} "
                    f"(month: {month_copied:,}, total: {total_copied:,}, "
                    f"rate: {rate:,.0f} rows/s)",
                    ending="\r",
                )

                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

            self.stdout.write(f"\n  Month complete: {month_copied:,} rows copied.")

        # Catch-all: copy any remaining rows outside the defined month ranges
        # (goes to the DEFAULT partition)
        if not target_month:
            self.stdout.write(
                self.style.NOTICE(
                    "\n--- Processing remaining rows (outside defined ranges) ---"
                )
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT MIN(id), MAX(id), COUNT(*) FROM amc_characterlocation "
                    'WHERE "timestamp" < %s OR "timestamp" >= %s',
                    [MONTHS[0][0], MONTHS[-1][1]],
                )
                min_id, max_id, remaining_count = cursor.fetchone()

            if min_id is not None and remaining_count > 0:
                self.stdout.write(
                    f"  ID range: {min_id} — {max_id}, rows: {remaining_count:,}"
                )
                remaining_copied = 0
                current_id = min_id
                while current_id <= max_id:
                    batch_end = current_id + batch_size
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "INSERT INTO amc_characterlocation_new "
                            '(id, "timestamp", character_id, location, vehicle_key) '
                            'SELECT id, "timestamp", character_id, location, vehicle_key '
                            "FROM amc_characterlocation "
                            "WHERE id >= %s AND id < %s "
                            'AND ("timestamp" < %s OR "timestamp" >= %s) '
                            'ON CONFLICT ("timestamp", character_id) DO NOTHING',
                            [current_id, batch_end, MONTHS[0][0], MONTHS[-1][1]],
                        )
                        rows_inserted = cursor.rowcount
                    remaining_copied += rows_inserted
                    total_copied += rows_inserted
                    current_id = batch_end
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)
                self.stdout.write(
                    f"\n  Remaining rows complete: {remaining_copied:,} rows copied."
                )
            else:
                self.stdout.write("  No remaining rows outside defined ranges.")

        elapsed = time.time() - start_time
        self.stdout.write(
            self.style.SUCCESS(
                f"\nBackfill complete: {total_copied:,} rows copied in {elapsed:.0f}s."
            )
        )

        # Final count comparison
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM amc_characterlocation")
            old_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM amc_characterlocation_new")
            new_count = cursor.fetchone()[0]

        diff = abs(old_count - new_count)
        self.stdout.write(f"Old table: {old_count:,} rows")
        self.stdout.write(f"New table: {new_count:,} rows")
        self.stdout.write(f"Difference: {diff:,} rows")

        if diff <= 100:
            self.stdout.write(
                self.style.SUCCESS(
                    "Row counts match (within tolerance). "
                    "Safe to run swap_partitioned_locations."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"Row count difference is {diff:,}. "
                    "This may indicate the backfill is incomplete. "
                    "Re-run the backfill or investigate before swapping."
                )
            )
