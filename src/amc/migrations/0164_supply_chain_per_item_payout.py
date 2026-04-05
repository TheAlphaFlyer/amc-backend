from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0163_shortcutzone"),
    ]

    operations = [
        # SupplyChainEvent: rename total_prize → reward_per_item
        migrations.RenameField(
            model_name="supplychainevent",
            old_name="total_prize",
            new_name="reward_per_item",
        ),
        # SupplyChainEvent: remove per_delivery_bonus_pct
        migrations.RemoveField(
            model_name="supplychainevent",
            name="per_delivery_bonus_pct",
        ),
        # SupplyChainEvent: remove escrowed_amount
        migrations.RemoveField(
            model_name="supplychainevent",
            name="escrowed_amount",
        ),
        # SupplyChainObjective: remove per_delivery_bonus_multiplier
        migrations.RemoveField(
            model_name="supplychainobjective",
            name="per_delivery_bonus_multiplier",
        ),
        # SupplyChainContribution: remove bonus_paid
        migrations.RemoveField(
            model_name="supplychaincontribution",
            name="bonus_paid",
        ),
        # Update the help_text on reward_per_item
        migrations.AlterField(
            model_name="supplychainevent",
            name="reward_per_item",
            field=models.PositiveBigIntegerField(
                help_text="Reward per unit of primary objective delivered"
            ),
        ),
    ]
