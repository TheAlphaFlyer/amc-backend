from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0185_server_teleport_log"),
    ]

    operations = [
        migrations.AddField(
            model_name="confiscation",
            name="deliveries",
            field=models.ManyToManyField(
                blank=True, related_name="confiscations", to="amc.delivery"
            ),
        ),
    ]
