# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('amc', '0149_deliverypoint_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='character',
            name='last_online',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
