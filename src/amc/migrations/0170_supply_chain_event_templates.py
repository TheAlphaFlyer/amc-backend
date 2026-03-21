from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0169_character_shortcut_zone_entered_at"),
    ]

    operations = [
        migrations.CreateModel(
            name="SupplyChainEventTemplate",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True)),
                (
                    "reward_per_item",
                    models.PositiveBigIntegerField(default=10000),
                ),
                ("duration_hours", models.FloatField(default=24.0)),
                ("enabled", models.BooleanField(default=True)),
            ],
        ),
        migrations.CreateModel(
            name="SupplyChainObjectiveTemplate",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "ceiling",
                    models.PositiveIntegerField(
                        blank=True,
                        help_text="Max rewardable quantity. Null = uncapped.",
                        null=True,
                    ),
                ),
                (
                    "reward_weight",
                    models.PositiveIntegerField(
                        default=10,
                        help_text="Relative weight for reward pool share (e.g. 40 for 40%)",
                    ),
                ),
                (
                    "is_primary",
                    models.BooleanField(
                        default=False,
                        help_text="Primary objectives define the main event goal",
                    ),
                ),
                (
                    "template",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="objectives",
                        to="amc.supplychaineventtemplate",
                    ),
                ),
                (
                    "cargos",
                    models.ManyToManyField(
                        blank=True,
                        related_name="sc_event_template_objectives",
                        to="amc.cargo",
                    ),
                ),
                (
                    "destination_points",
                    models.ManyToManyField(
                        blank=True,
                        related_name="sc_objective_templates_in",
                        to="amc.deliverypoint",
                    ),
                ),
                (
                    "source_points",
                    models.ManyToManyField(
                        blank=True,
                        related_name="sc_objective_templates_out",
                        to="amc.deliverypoint",
                    ),
                ),
            ],
        ),
    ]
