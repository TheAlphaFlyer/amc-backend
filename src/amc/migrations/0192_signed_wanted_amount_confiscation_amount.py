from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Allow negative values for Wanted.amount and Confiscation.amount so that
    a wrongful /setwanted (applied to an innocent civilian) can be represented
    as a negative bounty (-$100k penalty for the officer on arrest).
    """

    dependencies = [
        ("amc", "0191_add_wanted_amount"),
    ]

    operations = [
        migrations.AlterField(
            model_name="wanted",
            name="amount",
            field=models.BigIntegerField(
                default=0,
                help_text=(
                    "Cumulative illicit delivery payment for this wanted record. "
                    "Negative values indicate a wrongful wanted (innocent civilian)."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="confiscation",
            name="amount",
            field=models.IntegerField(
                help_text=(
                    "Confiscated amount in dollars. "
                    "Negative values indicate wrongful arrest compensation paid to the suspect."
                ),
            ),
        ),
    ]
