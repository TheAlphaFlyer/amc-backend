from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0157_update_oak_logs_subsidy"),
    ]

    operations = [
        migrations.CreateModel(
            name="PolicePatrolLog",
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
                ("timestamp", models.DateTimeField()),
                ("patrol_point_id", models.IntegerField()),
                ("data", models.JSONField(blank=True, null=True)),
                (
                    "player",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="police_patrols",
                        to="amc.player",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="PolicePenaltyLog",
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
                ("timestamp", models.DateTimeField()),
                ("warning_only", models.BooleanField()),
                ("data", models.JSONField(blank=True, null=True)),
                (
                    "player",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="police_penalties",
                        to="amc.player",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="PoliceShiftLog",
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
                ("timestamp", models.DateTimeField()),
                (
                    "action",
                    models.CharField(
                        choices=[("START", "Started Shift"), ("END", "Ended Shift")],
                        max_length=5,
                    ),
                ),
                ("data", models.JSONField(blank=True, null=True)),
                (
                    "player",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="police_shifts",
                        to="amc.player",
                    ),
                ),
            ],
        ),
    ]
