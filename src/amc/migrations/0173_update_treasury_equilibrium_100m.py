from django.db import migrations


def update_treasury_equilibrium(apps, schema_editor):
    JobPostingConfig = apps.get_model("amc", "JobPostingConfig")
    JobPostingConfig.objects.filter(pk=1, treasury_equilibrium=50_000_000).update(
        treasury_equilibrium=100_000_000
    )


def revert_treasury_equilibrium(apps, schema_editor):
    JobPostingConfig = apps.get_model("amc", "JobPostingConfig")
    JobPostingConfig.objects.filter(pk=1, treasury_equilibrium=100_000_000).update(
        treasury_equilibrium=50_000_000
    )


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0172_character_credit_score_and_more"),
    ]

    operations = [
        migrations.RunPython(
            update_treasury_equilibrium,
            revert_treasury_equilibrium,
        ),
    ]
