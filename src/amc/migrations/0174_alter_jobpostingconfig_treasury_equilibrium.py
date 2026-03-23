from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0173_update_treasury_equilibrium_100m"),
    ]

    operations = [
        migrations.AlterField(
            model_name="jobpostingconfig",
            name="treasury_equilibrium",
            field=models.PositiveBigIntegerField(
                default=100000000,
                help_text="Treasury balance at which spending is 'normal' (multiplier = 1.0)",
            ),
        ),
    ]
