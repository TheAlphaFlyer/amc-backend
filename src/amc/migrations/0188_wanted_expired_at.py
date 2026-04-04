from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0187_wanted"),
    ]

    operations = [
        migrations.AddField(
            model_name="wanted",
            name="expired_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
