import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0176_newsitem"),
    ]

    operations = [
        migrations.CreateModel(
            name="CriminalRecord",
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
                ("reason", models.CharField(max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                (
                    "character",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="criminal_records",
                        to="amc.character",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["character", "expires_at"],
                        name="amc_crimina_charact_idx",
                    ),
                ],
            },
        ),
    ]
