from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('amc', '0145_rescuerequest_location'),
    ]

    operations = [
        migrations.AlterField(
            model_name='deliverypoint',
            name='type',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AddField(
            model_name='deliverypoint',
            name='removed',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='deliveryjobtemplate',
            name='enabled',
            field=models.BooleanField(default=True, help_text='Disabled templates are skipped during job posting'),
        ),
    ]
