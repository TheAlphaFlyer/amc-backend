from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0167_add_max_posts_per_tick"),
    ]

    operations = [
        migrations.AddField(
            model_name="character",
            name="in_shortcut_zone",
            field=models.BooleanField(default=False),
        ),
    ]
