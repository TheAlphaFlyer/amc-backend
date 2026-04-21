import django.contrib.gis.db.models.fields as gis_models
from django.db import migrations
from django.contrib.gis.geos import Point


def backfill_location(apps, schema_editor):
    CharacterVehicle = apps.get_model("amc", "CharacterVehicle")
    for cv in CharacterVehicle.objects.filter(spawn_on_restart=True):
        loc = cv.config.get("Location")
        if loc and "X" in loc and "Y" in loc and "Z" in loc:
            cv.location = Point(loc["X"], loc["Y"], loc["Z"], srid=0)
            cv.save(update_fields=["location"])


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0201_add_is_world_vehicle"),
    ]

    operations = [
        migrations.AddField(
            model_name="charactervehicle",
            name="location",
            field=gis_models.PointField(dim=3, null=True, blank=True, srid=0),
        ),
        migrations.RunPython(backfill_location, migrations.RunPython.noop),
    ]
