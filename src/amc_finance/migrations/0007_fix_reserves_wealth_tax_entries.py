"""Data migration: swap debit/credit on Sovereign Reserves wealth tax entries.

The wealth tax code was creating credit entries for Sovereign Reserves
but manually overriding the balance to increase. Standard double-entry
requires debits for ASSET increases. This migration swaps the existing
entries to match the corrected code.
"""

from django.db import migrations


def swap_reserves_wealth_tax_entries(apps, schema_editor):
    """Swap debit/credit on all Sovereign Reserves 'Wealth Tax' entries."""
    LedgerEntry = apps.get_model("amc_finance", "LedgerEntry")

    # All wealth tax entries on the Sovereign Reserves account
    # Currently: debit=0, credit=amount → should be: debit=amount, credit=0
    entries = LedgerEntry.objects.filter(
        account__name="Sovereign Reserves",
        journal_entry__description="Wealth Tax",
    )
    count = 0
    for entry in entries:
        old_credit = entry.credit
        entry.debit = old_credit
        entry.credit = 0
        entry.save(update_fields=["debit", "credit"])
        count += 1

    if count:
        print(
            f"\n  Swapped {count} Sovereign Reserves wealth tax entries (credit→debit)"
        )


def reverse_swap(apps, schema_editor):
    """Reverse: swap debit back to credit."""
    LedgerEntry = apps.get_model("amc_finance", "LedgerEntry")

    entries = LedgerEntry.objects.filter(
        account__name="Sovereign Reserves",
        journal_entry__description="Wealth Tax",
    )
    for entry in entries:
        old_debit = entry.debit
        entry.credit = old_debit
        entry.debit = 0
        entry.save(update_fields=["debit", "credit"])


class Migration(migrations.Migration):
    dependencies = [
        ("amc_finance", "0006_daily_treasury_snapshot"),
    ]

    operations = [
        migrations.RunPython(
            swap_reserves_wealth_tax_entries,
            reverse_code=reverse_swap,
        ),
    ]
