from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = (
        "Swap the old CharacterLocation table with the new partitioned table. "
        "Run this AFTER backfill_partitioned_locations has completed successfully."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            default=False,
            help="Skip safety checks and force the swap",
        )

    def handle(self, *args, **options):
        force = options["force"]

        # --- Pre-flight checks ---

        with connection.cursor() as cursor:
            # Check the new table exists
            cursor.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'amc_characterlocation_new'"
            )
            if not cursor.fetchone():
                self.stderr.write(
                    self.style.ERROR(
                        "Table amc_characterlocation_new does not exist. "
                        "Run migration 0154 and backfill first."
                    )
                )
                return

            # Check trigger is active (dual-write is working)
            cursor.execute(
                "SELECT 1 FROM pg_trigger WHERE tgname = 'charloc_dual_write_trigger'"
            )
            if not cursor.fetchone():
                self.stderr.write(
                    self.style.ERROR(
                        "Dual-write trigger not found. "
                        "Ensure migration 0154 has been applied."
                    )
                )
                return

            # Row count comparison
            cursor.execute("SELECT COUNT(*) FROM amc_characterlocation")
            old_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM amc_characterlocation_new")
            new_count = cursor.fetchone()[0]

        diff = abs(old_count - new_count)
        self.stdout.write(f"Old table: {old_count:,} rows")
        self.stdout.write(f"New table: {new_count:,} rows")
        self.stdout.write(f"Difference: {diff:,} rows")

        if diff > 100 and not force:
            self.stderr.write(
                self.style.ERROR(
                    f"Row count difference is {diff:,} — too large. "
                    "Complete the backfill first, or use --force to skip this check."
                )
            )
            return

        # Check newest row in new table is recent (trigger is working)
        with connection.cursor() as cursor:
            cursor.execute('SELECT MAX("timestamp") FROM amc_characterlocation_new')
            newest = cursor.fetchone()[0]

        if newest:
            self.stdout.write(f"Newest row in new table: {newest}")
        else:
            self.stderr.write(
                self.style.ERROR("New table has no rows. Backfill first.")
            )
            return

        if not force:
            confirm = input(
                "\nReady to swap tables. This is a quick operation (~100ms). "
                "Proceed? [y/N]: "
            )
            if confirm.lower() != "y":
                self.stdout.write("Aborted.")
                return

        # --- Perform the swap ---

        self.stdout.write(self.style.NOTICE("\nPerforming table swap..."))

        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s';")

                # Drop the dual-write trigger
                cursor.execute(
                    "DROP TRIGGER charloc_dual_write_trigger ON amc_characterlocation;"
                )
                cursor.execute("DROP FUNCTION charloc_dual_write();")

                # Atomic table swap
                cursor.execute(
                    "ALTER TABLE amc_characterlocation "
                    "RENAME TO amc_characterlocation_old;"
                )
                cursor.execute(
                    "ALTER TABLE amc_characterlocation_new "
                    "RENAME TO amc_characterlocation;"
                )

                # Rename old-table indexes out of the way
                cursor.execute(
                    "ALTER INDEX IF EXISTS charloc_char_ts_idx "
                    "RENAME TO charloc_char_ts_idx_old;"
                )
                cursor.execute(
                    "ALTER INDEX IF EXISTS unique_character_location "
                    "RENAME TO unique_character_location_old;"
                )
                cursor.execute(
                    "ALTER INDEX IF EXISTS amc_characterlocation_pkey "
                    "RENAME TO amc_characterlocation_pkey_old;"
                )

                # Rename new-table indexes to expected names
                cursor.execute(
                    "ALTER INDEX charloc_new_char_ts_idx RENAME TO charloc_char_ts_idx;"
                )
                cursor.execute(
                    "ALTER INDEX unique_character_location_new "
                    "RENAME TO unique_character_location;"
                )
                cursor.execute(
                    "ALTER INDEX pk_charloc_new RENAME TO amc_characterlocation_pkey;"
                )

                # Rename FK constraint
                cursor.execute(
                    "ALTER TABLE amc_characterlocation "
                    "RENAME CONSTRAINT fk_charloc_new_character "
                    "TO fk_charloc_character;"
                )

                # Reset ID sequence for IDENTITY column
                cursor.execute(
                    "SELECT setval("
                    "pg_get_serial_sequence('amc_characterlocation', 'id'), "
                    "COALESCE((SELECT MAX(id) FROM amc_characterlocation), 1)"
                    ")"
                )

        self.stdout.write(self.style.SUCCESS("Table swap complete!"))

        # --- Setup pg_partman (if available) ---

        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    DO $$ BEGIN
                    IF EXISTS (
                        SELECT 1 FROM pg_available_extensions WHERE name = 'pg_partman'
                    ) THEN
                        CREATE EXTENSION IF NOT EXISTS pg_partman;
                        PERFORM public.create_parent(
                            p_parent_table := 'public.amc_characterlocation',
                            p_control := 'timestamp',
                            p_interval := '1 month',
                            p_premake := 3
                        );
                        RAISE NOTICE 'pg_partman configured for automatic partition maintenance';
                    ELSE
                        RAISE NOTICE 'pg_partman not available — create future partitions manually';
                    END IF;
                    END $$;
                """)
            self.stdout.write(self.style.SUCCESS("pg_partman configured successfully."))
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(
                    f"pg_partman setup failed (non-critical): {e}\n"
                    "Create future partitions manually if needed."
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                "\nDone! The old table remains as amc_characterlocation_old. "
                "Drop it after 24-48h of verification:\n"
                "  DROP TABLE amc_characterlocation_old;"
            )
        )

        # Post-swap verification
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM amc_characterlocation "
                "WHERE \"timestamp\" > now() - interval '5 minutes'"
            )
            recent = cursor.fetchone()[0]

        self.stdout.write(f"\nRecent rows (last 5 min): {recent}")
        if recent > 0:
            self.stdout.write(
                self.style.SUCCESS("Writes are flowing to the partitioned table.")
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "No recent rows found — check that amc-worker is running."
                )
            )
