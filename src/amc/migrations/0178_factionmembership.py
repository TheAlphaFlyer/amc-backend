# Generated manually based on `makemigrations` output

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0177_criminal_record"),
    ]

    operations = [
        migrations.CreateModel(
            name="FactionMembership",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "faction",
                    models.CharField(
                        choices=[("cop", "Cop"), ("criminal", "Criminal")],
                        max_length=10,
                    ),
                ),
                ("joined_at", models.DateTimeField(auto_now_add=True)),
                (
                    "last_switched_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "player",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="faction_membership",
                        to="amc.player",
                    ),
                ),
            ],
        ),
    ]
