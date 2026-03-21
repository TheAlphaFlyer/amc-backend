"""Seed supply chain event templates from game data."""

from django.db import migrations


def seed_templates(apps, schema_editor):
    SupplyChainEventTemplate = apps.get_model("amc", "SupplyChainEventTemplate")
    SupplyChainObjectiveTemplate = apps.get_model("amc", "SupplyChainObjectiveTemplate")
    Cargo = apps.get_model("amc", "Cargo")
    DeliveryPoint = apps.get_model("amc", "DeliveryPoint")

    def get_cargos(*keys):
        return list(Cargo.objects.filter(key__in=keys))

    def get_points_by_name(*names):
        return list(DeliveryPoint.objects.filter(name__in=names))

    templates = [
        {
            "name": "Steel Rush",
            "description": "Export steel products through the harbor! Mine coal and iron ore, produce steel coils, and ship them out.",
            "reward_per_item": 10_000,
            "duration_hours": 48.0,
            "objectives": [
                {
                    "cargo_keys": ["SteelCoil_10t"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 200,
                    "reward_weight": 60,
                    "is_primary": True,
                },
                {
                    "cargo_keys": ["Coal", "IronOre"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 500,
                    "reward_weight": 40,
                    "is_primary": False,
                },
            ],
        },
        {
            "name": "Fuel Crisis",
            "description": "The gas stations are running dry! Haul crude oil to the refinery and deliver fuel across the map.",
            "reward_per_item": 10_000,
            "duration_hours": 24.0,
            "objectives": [
                {
                    "cargo_keys": ["Fuel"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 300,
                    "reward_weight": 60,
                    "is_primary": True,
                },
                {
                    "cargo_keys": ["CrudeOil"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 400,
                    "reward_weight": 40,
                    "is_primary": False,
                },
            ],
        },
        {
            "name": "Build the City",
            "description": "The city needs infrastructure! Deliver construction materials to build sites across the map.",
            "reward_per_item": 10_000,
            "duration_hours": 48.0,
            "objectives": [
                {
                    "cargo_keys": ["Concrete"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 300,
                    "reward_weight": 40,
                    "is_primary": True,
                },
                {
                    "cargo_keys": ["Cement", "WoodPlank_14ft_5t", "PlasticPipes_6m", "lHBeam_6m"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 500,
                    "reward_weight": 60,
                    "is_primary": False,
                },
            ],
        },
        {
            "name": "Feed the People",
            "description": "Supermarket shelves are empty! Deliver food products to keep the population fed.",
            "reward_per_item": 10_000,
            "duration_hours": 24.0,
            "objectives": [
                {
                    "cargo_keys": ["BreadPallet", "CheesePallet", "MeatBox"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 200,
                    "reward_weight": 60,
                    "is_primary": True,
                },
                {
                    "cargo_keys": ["CornPallet", "PotatoPallet", "CabbagePallet", "Milk"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 400,
                    "reward_weight": 40,
                    "is_primary": False,
                },
            ],
        },
        {
            "name": "Arms Race",
            "description": "Military supply boxes are needed at the warehouse. Coordinate logistics to fill the order.",
            "reward_per_item": 10_000,
            "duration_hours": 24.0,
            "objectives": [
                {
                    "cargo_keys": ["MilitarySupplyBox_01"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 100,
                    "reward_weight": 100,
                    "is_primary": True,
                },
            ],
        },
        {
            "name": "Factory Revival",
            "description": "Get the plastic factories back online! Deliver oil for processing and distribute plastic products.",
            "reward_per_item": 10_000,
            "duration_hours": 24.0,
            "objectives": [
                {
                    "cargo_keys": ["PlasticPallete"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 200,
                    "reward_weight": 60,
                    "is_primary": True,
                },
                {
                    "cargo_keys": ["Oil"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 300,
                    "reward_weight": 40,
                    "is_primary": False,
                },
            ],
        },
        {
            "name": "Lumber Run",
            "description": "The construction sites need wood! Haul logs to the lumber mill and deliver planks.",
            "reward_per_item": 10_000,
            "duration_hours": 24.0,
            "objectives": [
                {
                    "cargo_keys": ["WoodPlank_14ft_5t"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 200,
                    "reward_weight": 60,
                    "is_primary": True,
                },
                {
                    "cargo_keys": ["Log_20ft", "Log_Oak_12ft", "Log_Oak_24ft"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 400,
                    "reward_weight": 40,
                    "is_primary": False,
                },
            ],
        },
        {
            "name": "Terra Assembly",
            "description": "The Terra factory needs parts! Deliver all the raw materials to assemble the ultimate vehicle.",
            "reward_per_item": 10_000,
            "duration_hours": 72.0,
            "objectives": [
                {
                    "cargo_keys": ["Terra"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 50,
                    "reward_weight": 50,
                    "is_primary": True,
                },
                {
                    "cargo_keys": ["SteelCoil_10t", "lHBeam_6m", "PlasticPallete", "Oil", "Fuel"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 500,
                    "reward_weight": 50,
                    "is_primary": False,
                },
            ],
        },
        {
            "name": "Stone & Lime",
            "description": "The mining chain is in demand! Haul limestone and process it into quicklime and cement.",
            "reward_per_item": 10_000,
            "duration_hours": 24.0,
            "objectives": [
                {
                    "cargo_keys": ["Limestone"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 300,
                    "reward_weight": 50,
                    "is_primary": True,
                },
                {
                    "cargo_keys": ["LimestoneRock", "QuicklimePallet"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 400,
                    "reward_weight": 50,
                    "is_primary": False,
                },
            ],
        },
        {
            "name": "Harbor Export",
            "description": "Fill export orders at the harbor! Deliver H-beams and steel coils from the full production chain.",
            "reward_per_item": 10_000,
            "duration_hours": 48.0,
            "objectives": [
                {
                    "cargo_keys": ["lHBeam_6m", "SteelCoil_10t"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 200,
                    "reward_weight": 60,
                    "is_primary": True,
                },
                {
                    "cargo_keys": ["Coal", "IronOre"],
                    "dest_names": [],
                    "src_names": [],
                    "ceiling": 500,
                    "reward_weight": 40,
                    "is_primary": False,
                },
            ],
        },
    ]

    for tmpl_data in templates:
        tmpl = SupplyChainEventTemplate.objects.create(
            name=tmpl_data["name"],
            description=tmpl_data["description"],
            reward_per_item=tmpl_data["reward_per_item"],
            duration_hours=tmpl_data["duration_hours"],
        )

        for obj_data in tmpl_data["objectives"]:
            obj = SupplyChainObjectiveTemplate.objects.create(
                template=tmpl,
                ceiling=obj_data["ceiling"],
                reward_weight=obj_data["reward_weight"],
                is_primary=obj_data["is_primary"],
            )

            cargos = get_cargos(*obj_data["cargo_keys"])
            if cargos:
                obj.cargos.add(*cargos)

            if obj_data["dest_names"]:
                dest_points = get_points_by_name(*obj_data["dest_names"])
                if dest_points:
                    obj.destination_points.add(*dest_points)

            if obj_data["src_names"]:
                src_points = get_points_by_name(*obj_data["src_names"])
                if src_points:
                    obj.source_points.add(*src_points)


def remove_templates(apps, schema_editor):
    SupplyChainEventTemplate = apps.get_model("amc", "SupplyChainEventTemplate")
    SupplyChainEventTemplate.objects.filter(
        name__in=[
            "Steel Rush",
            "Fuel Crisis",
            "Build the City",
            "Feed the People",
            "Arms Race",
            "Factory Revival",
            "Lumber Run",
            "Terra Assembly",
            "Stone & Lime",
            "Harbor Export",
        ]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0170_supply_chain_event_templates"),
    ]

    operations = [
        migrations.RunPython(seed_templates, reverse_code=remove_templates),
    ]
