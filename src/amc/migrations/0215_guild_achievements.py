import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0214_guild_requirement_bonus_pct_float"),
    ]

    operations = [
        migrations.AddField(
            model_name="guild",
            name="discord_thread_id",
            field=models.CharField(
                max_length=32, null=True, blank=True, unique=True
            ),
        ),
        migrations.CreateModel(
            name="GuildAchievement",
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
                ("name", models.CharField(max_length=128)),
                ("description", models.TextField(blank=True)),
                ("icon", models.CharField(blank=True, max_length=50)),
                ("order", models.PositiveIntegerField(default=0)),
                ("criteria", models.JSONField()),
                (
                    "guild",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="achievements",
                        to="amc.guild",
                    ),
                ),
            ],
            options={
                "ordering": ["order", "id"],
            },
        ),
        migrations.CreateModel(
            name="GuildCharacterAchievement",
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
                ("progress", models.IntegerField(default=0)),
                ("completed_at", models.DateTimeField(null=True, blank=True)),
                (
                    "achievement",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="completions",
                        to="amc.guildachievement",
                    ),
                ),
                (
                    "guild_character",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="achievements",
                        to="amc.guildcharacter",
                    ),
                ),
            ],
            options={
                "unique_together": {("guild_character", "achievement")},
            },
        ),
    ]
