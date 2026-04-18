from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0197_alter_characterlocation_vehicle_key_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobpostingconfig",
            name="treasury_cap_ratio",
            field=models.FloatField(
                default=4.0,
                help_text="Ratio at which above-equilibrium multiplier reaches 2.0 (higher = slower growth)",
            ),
        ),
        migrations.AlterField(
            model_name="jobpostingconfig",
            name="treasury_sensitivity",
            field=models.FloatField(
                default=1.5,
                help_text="How aggressively spending changes with treasury balance (higher = steeper curve)",
            ),
        ),
    ]
