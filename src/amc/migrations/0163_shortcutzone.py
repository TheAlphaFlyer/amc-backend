from django.contrib.gis.geos import Point
from django.db import migrations, models
import django.contrib.gis.db.models.fields


def seed_shortcut_zones(apps, schema_editor):
    ShortcutZone = apps.get_model("amc", "ShortcutZone")

    # Recreate the old hardcoded zones as polygons
    gwangjin_poly = Point(359285, 892222, srid=3857).buffer(10000)
    migeum_poly = Point(227878, 449541, srid=3857).buffer(6000)

    ShortcutZone.objects.get_or_create(
        name="Gwangjin Shortcut",
        defaults={
            "polygon": gwangjin_poly,
            "active": True,
            "description": "Original Gwangjin shortcut zone",
        },
    )
    ShortcutZone.objects.get_or_create(
        name="Migeum Shortcut",
        defaults={
            "polygon": migeum_poly,
            "active": True,
            "description": "Original Migeum shortcut zone",
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0162_supplychainevent_supplychainobjective_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ShortcutZone",
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
                ("name", models.CharField(max_length=200)),
                (
                    "polygon",
                    django.contrib.gis.db.models.fields.PolygonField(dim=2, srid=3857),
                ),
                ("active", models.BooleanField(default=True)),
                ("description", models.TextField(blank=True)),
            ],
        ),
        migrations.RunPython(seed_shortcut_zones, migrations.RunPython.noop),
    ]
