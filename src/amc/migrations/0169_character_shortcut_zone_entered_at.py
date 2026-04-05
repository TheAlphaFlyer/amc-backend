from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0168_character_in_shortcut_zone"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="character",
            name="in_shortcut_zone",
        ),
        migrations.AddField(
            model_name="character",
            name="shortcut_zone_entered_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
