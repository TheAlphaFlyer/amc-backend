# Restore type field that was incorrectly removed in 0148

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('amc', '0148_unique_delivery_point_storage'),
    ]

    operations = [
        migrations.AddField(
            model_name='deliverypoint',
            name='type',
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
