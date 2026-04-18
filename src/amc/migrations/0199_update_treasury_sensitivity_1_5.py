from django.db import migrations


def update_sensitivity(apps, schema_editor):
    JobPostingConfig = apps.get_model("amc", "JobPostingConfig")
    JobPostingConfig.objects.filter(treasury_sensitivity=0.5).update(
        treasury_sensitivity=1.5
    )


def revert_sensitivity(apps, schema_editor):
    JobPostingConfig = apps.get_model("amc", "JobPostingConfig")
    JobPostingConfig.objects.filter(treasury_sensitivity=1.5).update(
        treasury_sensitivity=0.5
    )


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0198_jobpostingconfig_treasury_cap_ratio"),
    ]

    operations = [
        migrations.RunPython(update_sensitivity, revert_sensitivity),
    ]
