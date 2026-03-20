from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0166_voucher"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobpostingconfig",
            name="max_posts_per_tick",
            field=models.PositiveIntegerField(
                default=3,
                help_text="Maximum number of new jobs to post per cron tick (rate limit)",
            ),
        ),
    ]
