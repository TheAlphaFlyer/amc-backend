from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0186_confiscation_deliveries"),
    ]

    operations = [
        migrations.AddField(
            model_name="rescuerequest",
            name="last_reminded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
