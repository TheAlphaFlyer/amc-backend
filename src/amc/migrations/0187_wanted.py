import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('amc', '0186_confiscation_deliveries'),
    ]

    operations = [
        migrations.CreateModel(
            name='Wanted',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('protection_remaining', models.PositiveIntegerField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('character', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='wanted_status', to='amc.character')),
            ],
            options={
                'verbose_name_plural': 'wants',
            },
        ),
    ]
