from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0175_character_crossover_warning_sent_at"),
    ]

    operations = [
        migrations.CreateModel(
            name="NewsItem",
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
                ("title", models.CharField(max_length=200)),
                ("body", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "expires_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="Leave blank to auto-expire 7 days after creation.",
                        null=True,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
