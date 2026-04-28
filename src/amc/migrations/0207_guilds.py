from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0206_populate_teleportportals"),
    ]

    operations = [
        migrations.CreateModel(
            name="Guild",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=128, unique=True)),
                ("abbreviation", models.CharField(max_length=10, unique=True)),
                ("vehicle_key", models.CharField(max_length=100)),
                ("engine_part_key", models.CharField(blank=True, max_length=200, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("decal", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="guilds", to="amc.vehicledecal")),
            ],
        ),
        migrations.CreateModel(
            name="GuildSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("started_at", models.DateTimeField()),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("guild", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sessions", to="amc.guild")),
                ("character", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="guild_sessions", to="amc.character")),
            ],
        ),
        migrations.AddIndex(
            model_name="guildsession",
            index=models.Index(fields=["character", "ended_at"], name="guildsession_char_end_idx"),
        ),
        migrations.AddConstraint(
            model_name="guildsession",
            constraint=models.UniqueConstraint(
                fields=("character",),
                condition=models.Q(("ended_at__isnull", True)),
                name="unique_active_guild_session",
            ),
        ),
        migrations.CreateModel(
            name="GuildCharacter",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("level", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("guild", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="characters", to="amc.guild")),
                ("character", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="guild_memberships", to="amc.character")),
            ],
        ),
        migrations.AddConstraint(
            model_name="guildcharacter",
            constraint=models.UniqueConstraint(
                fields=("guild", "character"),
                name="unique_guild_character",
            ),
        ),
    ]
