from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0152_gov_employee_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="JobPostingConfig",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "target_success_rate",
                    models.FloatField(
                        default=0.50,
                        help_text="Target job completion rate (0.0-1.0). Jobs scale up above this, down below.",
                    ),
                ),
                (
                    "min_multiplier",
                    models.FloatField(
                        default=0.5,
                        help_text="Minimum adaptive multiplier (scales down job count when success rate is low)",
                    ),
                ),
                (
                    "max_multiplier",
                    models.FloatField(
                        default=2.0,
                        help_text="Maximum adaptive multiplier (scales up job count when success rate is high)",
                    ),
                ),
                (
                    "players_per_job",
                    models.IntegerField(
                        default=10,
                        help_text="Base formula: 1 job per N players",
                    ),
                ),
                (
                    "min_base_jobs",
                    models.IntegerField(
                        default=2,
                        help_text="Minimum number of base active jobs regardless of player count",
                    ),
                ),
                (
                    "posting_rate_multiplier",
                    models.FloatField(
                        default=1.0,
                        help_text="Global multiplier on posting chance (0.5 = half rate, 2.0 = double rate)",
                    ),
                ),
            ],
            options={
                "verbose_name": "Job Posting Configuration",
                "verbose_name_plural": "Job Posting Configuration",
            },
        ),
    ]
