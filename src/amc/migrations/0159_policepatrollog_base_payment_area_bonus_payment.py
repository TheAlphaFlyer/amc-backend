from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0158_policepatrollog_policepenaltylog_policeshiftlog"),
    ]

    operations = [
        migrations.AddField(
            model_name="policepatrollog",
            name="base_payment",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="policepatrollog",
            name="area_bonus_payment",
            field=models.IntegerField(default=0),
        ),
    ]
