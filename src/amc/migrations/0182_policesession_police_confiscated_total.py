from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0181_add_criminal_laundered_total"),
    ]

    operations = [
        migrations.AddField(
            model_name="character",
            name="police_confiscated_total",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.CreateModel(
            name="PoliceSession",
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
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                (
                    "character",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="police_sessions",
                        to="amc.character",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["character", "ended_at"],
                        name="amc_polices_charact_idx",
                    ),
                ],
            },
        ),
    ]
