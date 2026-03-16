from decimal import Decimal
from django.db import migrations


def update_oak_logs_subsidy(apps, schema_editor):
    SubsidyRule = apps.get_model("amc", "SubsidyRule")
    SubsidyRule.objects.filter(name="Oak Logs").update(
        reward_value=Decimal("1.25"),
        scales_with_damage=False,
    )


def revert_oak_logs_subsidy(apps, schema_editor):
    SubsidyRule = apps.get_model("amc", "SubsidyRule")
    SubsidyRule.objects.filter(name="Oak Logs").update(
        reward_value=Decimal("2.50"),
        scales_with_damage=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0156_treasury_equilibrium_config"),
    ]

    operations = [
        migrations.RunPython(update_oak_logs_subsidy, revert_oak_logs_subsidy),
    ]
