from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0213_guild_welcome_message"),
    ]

    operations = [
        migrations.AlterField(
            model_name="guildcargorequirement",
            name="bonus_pct",
            field=models.FloatField(default=0),
        ),
        migrations.AlterField(
            model_name="guildpassengerrequirement",
            name="bonus_pct",
            field=models.FloatField(default=0),
        ),
    ]
