from django.db import migrations, models
import django.db.models.deletion


def migrate_guild_vehicles_forward(apps, schema_editor):
    Guild = apps.get_model("amc", "Guild")
    GuildVehicle = apps.get_model("amc", "GuildVehicle")
    GuildVehiclePart = apps.get_model("amc", "GuildVehiclePart")

    for guild in Guild.objects.all():
        gv = GuildVehicle.objects.create(
            guild=guild,
            vehicle_key=guild.vehicle_key,
            decal=guild.decal,
        )
        if guild.engine_part_key:
            GuildVehiclePart.objects.create(
                guild_vehicle=gv,
                part_key=guild.engine_part_key,
            )


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0207_guilds"),
    ]

    operations = [
        migrations.CreateModel(
            name="GuildVehicle",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("vehicle_key", models.CharField(max_length=100)),
                ("guild", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="vehicles", to="amc.guild")),
                ("decal", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="guild_vehicles", to="amc.vehicledecal")),
            ],
        ),
        migrations.AddConstraint(
            model_name="guildvehicle",
            constraint=models.UniqueConstraint(
                fields=("guild", "vehicle_key"),
                name="unique_guild_vehicle_key",
            ),
        ),
        migrations.CreateModel(
            name="GuildVehiclePart",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("part_key", models.CharField(max_length=200)),
                ("guild_vehicle", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="parts", to="amc.guildvehicle")),
            ],
        ),
        migrations.AddConstraint(
            model_name="guildvehiclepart",
            constraint=models.UniqueConstraint(
                fields=("guild_vehicle", "part_key"),
                name="unique_guild_vehicle_part",
            ),
        ),
        migrations.RunPython(migrate_guild_vehicles_forward, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="guild",
            name="vehicle_key",
        ),
        migrations.RemoveField(
            model_name="guild",
            name="engine_part_key",
        ),
        migrations.RemoveField(
            model_name="guild",
            name="decal",
        ),
    ]
