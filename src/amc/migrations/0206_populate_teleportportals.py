from django.contrib.gis.geos import Point
from django.db import migrations


def populate_teleport_portals(apps, schema_editor):
    TeleportPortal = apps.get_model("amc", "TeleportPortal")
    TeleportPortal.objects.bulk_create(
        [
            TeleportPortal(
                name="Meehoi House Entrance",
                source=Point(x=69664.27, y=651361.93, z=-8214.26, srid=0),
                source_radius=150,
                target=Point(x=68205.77, y=651084.19, z=-7000.43, srid=0),
                active=True,
            ),
            TeleportPortal(
                name="Meehoi House Exit",
                source=Point(x=68119.18, y=650502.15, z=-6909.83, srid=0),
                source_radius=120,
                target=Point(x=67912.23, y=650236.37, z=-8512.19, srid=0),
                active=True,
            ),
            TeleportPortal(
                name="Rooftop Bar Entrance",
                source=Point(x=-67173.12, y=150561.7, z=-20646.4, srid=0),
                source_radius=150,
                target=Point(x=-66531.10, y=150471.73, z=-19706.87, srid=0),
                active=True,
            ),
            TeleportPortal(
                name="Rooftop Bar Exit",
                source=Point(x=-66733.74, y=150411.51, z=-19703.15, srid=0),
                source_radius=120,
                target=Point(x=-67245.74, y=150831.6, z=-20646.85, srid=0),
                active=True,
            ),
        ]
    )


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0205_teleportportal"),
    ]

    operations = [
        migrations.RunPython(populate_teleport_portals, migrations.RunPython.noop),
    ]
