# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False  # Required for CREATE INDEX CONCURRENTLY

    dependencies = [
        ("amc", "0147_character_last_location_character_last_vehicle_key_and_more"),
    ]

    operations = [
        # Remove duplicates before adding the unique constraint.
        # Keep the row with the highest id for each (delivery_point, kind, cargo_key).
        # Table has ~4k rows so this is near-instant.
        migrations.RunSQL(
            sql="""
                DELETE FROM amc_deliverypointstorage
                WHERE id NOT IN (
                    SELECT MAX(id)
                    FROM amc_deliverypointstorage
                    GROUP BY delivery_point_id, kind, cargo_key
                );
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        # Use CONCURRENTLY to avoid holding ACCESS EXCLUSIVE lock
        migrations.RunSQL(
            sql='CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "unique_delivery_point_storage" ON "amc_deliverypointstorage" ("delivery_point_id", "kind", "cargo_key")',
            reverse_sql='DROP INDEX CONCURRENTLY IF EXISTS "unique_delivery_point_storage"',
            state_operations=[
                migrations.AddConstraint(
                    model_name="deliverypointstorage",
                    constraint=models.UniqueConstraint(
                        fields=["delivery_point", "kind", "cargo_key"],
                        name="unique_delivery_point_storage",
                    ),
                ),
            ],
        ),
        # Column drop is metadata-only in PostgreSQL (no table rewrite)
        migrations.RemoveField(
            model_name="deliverypoint",
            name="type",
        ),
    ]
