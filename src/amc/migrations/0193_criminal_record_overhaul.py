"""Criminal Record overhaul migration.

Changes:
  CriminalRecord:
    - Remove expires_at (replaced by cleared_at with NULL = active semantics)
    - Add cleared_at (nullable DateTimeField — NULL means active)
    - Add amount (BigIntegerField — permanent total of illicit delivery payments)
    - Add confiscatable_amount (BigIntegerField — decaying sum, reduced by cron when online)
    - Add cleared_by_arrest FK to Confiscation
    - Update index: (character, cleared_at) instead of (character, expires_at)
    - Add UniqueConstraint on (character) WHERE cleared_at IS NULL

  Delivery:
    - Remove wanted FK
    - Add criminal_record FK to CriminalRecord

Data migration:
    - expired CriminalRecords (old expires_at < now): cleared_at = expires_at
    - active CriminalRecords (old expires_at >= now): cleared_at = NULL
"""

from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0192_signed_wanted_amount_confiscation_amount"),
    ]

    operations = [
        # ── Step 1: Remove old index on CriminalRecord ───────────────────────
        migrations.RemoveIndex(
            model_name="criminalrecord",
            name="amc_crimina_charact_0827c6_idx",
        ),

        # ── Step 2: Rename expires_at → cleared_at on CriminalRecord ─────────
        migrations.RenameField(
            model_name="criminalrecord",
            old_name="expires_at",
            new_name="cleared_at",
        ),

        # ── Step 3: Make cleared_at nullable (active = NULL) ─────────────────
        migrations.AlterField(
            model_name="criminalrecord",
            name="cleared_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="NULL = active record. Set on arrest to close the record.",
            ),
        ),

        # ── Step 4: Data migration — set cleared_at correctly ────────────────
        # expired records: keep cleared_at as-is (it already has the past datetime)
        # active records: set cleared_at = NULL
        migrations.RunSQL(
            sql="""
                UPDATE amc_criminalrecord
                SET cleared_at = NULL
                WHERE cleared_at > NOW();
            """,
            reverse_sql="""
                UPDATE amc_criminalrecord
                SET cleared_at = NOW() + INTERVAL '7 days'
                WHERE cleared_at IS NULL;
            """,
        ),

        # ── Step 5: Add new CriminalRecord fields ─────────────────────────────
        migrations.AddField(
            model_name="criminalrecord",
            name="amount",
            field=models.BigIntegerField(
                default=0,
                help_text="Permanent total of illicit delivery payments during this record.",
            ),
        ),
        migrations.AddField(
            model_name="criminalrecord",
            name="confiscatable_amount",
            field=models.BigIntegerField(
                default=0,
                help_text="Decaying sum of illicit delivery payments. Reduced by cron when online (half-life 4h).",
            ),
        ),
        migrations.AddField(
            model_name="criminalrecord",
            name="cleared_by_arrest",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="cleared_records",
                to="amc.confiscation",
            ),
        ),

        # ── Step 6: Add new index on (character, cleared_at) ─────────────────
        migrations.AddIndex(
            model_name="criminalrecord",
            index=models.Index(
                fields=["character", "cleared_at"],
                name="amc_crimina_charact_62d906_idx",
            ),
        ),

        # ── Step 7: Add UniqueConstraint (one active record per character) ────
        migrations.AddConstraint(
            model_name="criminalrecord",
            constraint=models.UniqueConstraint(
                fields=["character"],
                condition=Q(cleared_at__isnull=True),
                name="unique_active_criminal_record_per_character",
            ),
        ),

        # ── Step 8: Remove Delivery.wanted FK ────────────────────────────────
        migrations.RemoveField(
            model_name="delivery",
            name="wanted",
        ),

        # ── Step 9: Add Delivery.criminal_record FK ───────────────────────────
        migrations.AddField(
            model_name="delivery",
            name="criminal_record",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="deliveries",
                to="amc.criminalrecord",
            ),
        ),
    ]
