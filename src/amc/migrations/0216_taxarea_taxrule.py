import django.contrib.gis.db.models.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0215_guild_achievements"),
    ]

    operations = [
        migrations.CreateModel(
            name="TaxArea",
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
                (
                    "polygon",
                    django.contrib.gis.db.models.fields.PolygonField(srid=3857),
                ),
                ("description", models.TextField(blank=True)),
            ],
        ),
        migrations.CreateModel(
            name="TaxRule",
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
                ("active", models.BooleanField(default=True)),
                (
                    "priority",
                    models.IntegerField(
                        default=0, help_text="Higher number = evaluated first"
                    ),
                ),
                ("requires_on_time", models.BooleanField(default=False)),
                (
                    "tax_type",
                    models.CharField(
                        choices=[
                            ("PERCENTAGE", "Percentage"),
                            ("FLAT", "Flat Amount"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "tax_value",
                    models.DecimalField(
                        decimal_places=2,
                        help_text=(
                            "Percentage (e.g. 0.10 for 10%) or Flat Amount, "
                            "deducted from base cargo payment"
                        ),
                        max_digits=12,
                    ),
                ),
                (
                    "scales_with_damage",
                    models.BooleanField(
                        default=False,
                        help_text="If true, multiplies tax by health %",
                    ),
                ),
                (
                    "collected",
                    models.DecimalField(
                        decimal_places=2,
                        default=0,
                        help_text="Lifetime amount collected by this rule",
                        max_digits=16,
                    ),
                ),
                (
                    "cargos",
                    models.ManyToManyField(
                        blank=True,
                        help_text="If empty, applies to ALL cargos",
                        related_name="tax_rules",
                        to="amc.cargo",
                    ),
                ),
                (
                    "source_areas",
                    models.ManyToManyField(
                        blank=True,
                        related_name="source_tax_rules",
                        to="amc.taxarea",
                    ),
                ),
                (
                    "destination_areas",
                    models.ManyToManyField(
                        blank=True,
                        related_name="destination_tax_rules",
                        to="amc.taxarea",
                    ),
                ),
                (
                    "source_delivery_points",
                    models.ManyToManyField(
                        blank=True,
                        related_name="source_tax_rules",
                        to="amc.deliverypoint",
                    ),
                ),
                (
                    "destination_delivery_points",
                    models.ManyToManyField(
                        blank=True,
                        related_name="destination_tax_rules",
                        to="amc.deliverypoint",
                    ),
                ),
            ],
        ),
    ]
